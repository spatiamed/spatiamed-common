"""PHI-safe capture of error LOG LINES (as opposed to exceptions).

Two paths, both fail closed:

1. Message-only ERROR records (``log.error("realtime_pipeline_error", provider=...)``,
   stdlib ``logger.error("...")``): the SDK's LoggingIntegration turns them into an
   event, and ``scrub.scrub_event`` rebuilds it here as a MINIMAL event. The raw log
   message, its params and the structlog kwargs NEVER ship. What ships:
     - message = an event key: the structlog ``event`` when it is a plain lowercase
       identifier (a code constant), else ``<logger>.<function>`` (code location);
     - contexts.log_fields = only the ALLOWLISTED structured fields below;
     - fingerprint = [event_key, error_class] so identical lines group;
   and the event is rate limited per key and per process.

2. ``structlog_capture_exc_info``: a structlog processor. structlog's stdlib bridge
   (``ProcessorFormatter.wrap_for_formatter``) keeps ``exc_info`` inside the event
   dict, so the stdlib record carries no exception and path 1 would reduce
   ``log.error("database_error", exc_info=e)`` to a message. The processor sends the
   exception itself (scrubbed by scrub_event like any exception) and marks the event
   dict so the stdlib copy is skipped (no duplicate).

Does not import sentry_sdk at module import time.
"""

from __future__ import annotations

import math
import re
import sys
import threading
import time
from collections.abc import Callable, MutableMapping
from types import TracebackType
from typing import Any

# Set on a structlog event dict once its exception went to the SDK; scrub_event drops
# the stdlib copy of a record whose msg carries it.
CAPTURED_MARKER = "_sentry_captured"

# A structlog event key is used as the issue title only when it looks like a code
# constant. Lowercase identifiers only: an f-string event (f"lookup failed for {name}")
# has spaces/capitals and falls back to the code location. Names are not
# regex-redactable, so this shape gate is the control for the title.
_EVENT_KEY = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
_LOGGER_NAME = re.compile(r"^[A-Za-z0-9_.:<>-]{1,120}$")
# Allowlisted string values must be identifier-shaped (class names, provider names,
# enum-like stages); free text is dropped, never truncated.
_SAFE_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
# Short status-like strings ("402", "5xx"); never long digit runs (phones, Aadhaar).
_STATUS_VALUE = re.compile(r"^[0-9]{1,3}[a-z]{0,2}$")
_TAG_MAX = 200  # Sentry tag value limit
_HASH_VALUE = re.compile(r"^[0-9a-f]{8,64}$")
# Numbers: phones (10 digits), Aadhaar (12) and other identifiers are >= 10^9, so any
# int/float with |value| >= 10^7 is dropped. 10^7 (not 10^6) because duration_ms /
# elapsed_ms for a long call legitimately exceed 10^6 ms (~17 min); counts, attempts
# and status codes are far below either cap. Non-finite floats are dropped too.
NUMERIC_ABS_LIMIT = 10**7
# Strings: any run of more than 6 digits is an identifier, never a code constant.
_LONG_DIGITS = re.compile(r"[0-9]{7,}")
# Hashes: a hex digest has letters; a run of 10+ digits is a phone/Aadhaar shape.
_HASH_DIGIT_RUN = re.compile(r"[0-9]{10,}")

# The ONLY structured fields a message-only log event may carry. Everything else in
# the structlog dict / stdlib `extra` is dropped. Add a field only if its values are
# code constants, numbers or booleans: never names, phones, transcripts, free text.
ALLOWED_LOG_FIELDS: frozenset[str] = frozenset(
    {
        # exception class names (also the fingerprint's error_class)
        "error_type",
        "error_class",
        "exc_type",
        # HTTP / provider status codes
        "status",
        "status_code",
        "http_status",
        # code identifiers: which service / vendor / pipeline stage failed
        "service",
        "provider",
        "processor",
        "component",
        "stage",
        "operation",
        # booleans
        "fatal",
        "retryable",
        # counts and timings
        "count",
        "attempt",
        "attempts",
        "retries",
        "duration_ms",
        "elapsed_ms",
        "latency_ms",
    }
)
# Plus any key ending in `_hash` whose value is a lowercase hex digest (hashed ids).
_HASH_SUFFIX = "_hash"
_ERROR_CLASS_FIELDS = ("error_type", "error_class", "exc_type")

# Top-level event keys kept on a minimal event. logentry / message / extra / threads
# and anything else the SDK or a future integration adds are dropped.
_EVENT_KEEP = frozenset(
    {
        "event_id",
        "timestamp",
        "level",
        "logger",
        "platform",
        "environment",
        "release",
        "dist",
        "server_name",
        "sdk",
        "tags",
        "contexts",
        "breadcrumbs",
        "request",
        "user",
        "transaction",
        "transaction_info",
    }
)

# Rate limits for message-only log events (exceptions are not limited).
PER_KEY_PER_MINUTE = 5
TOTAL_PER_MINUTE = 30


class _RateLimiter:
    """Fixed 60 s windows: <= per_key events per key and <= total events per window."""

    def __init__(
        self,
        per_key: int,
        total: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_key = per_key
        self.total = total
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()
        self._start = clock()
        self._counts: dict[str, int] = {}
        self._total = 0

    def reset(self) -> None:
        with self._lock:
            self._start = self.clock()
            self._counts.clear()
            self._total = 0

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self.clock()
            if now - self._start >= self.window:
                self._start = now
                self._counts.clear()
                self._total = 0
            if self._total >= self.total or self._counts.get(key, 0) >= self.per_key:
                return False
            self._counts[key] = self._counts.get(key, 0) + 1
            self._total += 1
            return True


rate_limiter = _RateLimiter(PER_KEY_PER_MINUTE, TOTAL_PER_MINUTE)


def _safe_logger(name: object) -> str:
    return name if isinstance(name, str) and _LOGGER_NAME.match(name) else "unknown"


def _event_key(record: Any) -> str:
    msg = getattr(record, "msg", None)
    if isinstance(msg, MutableMapping):
        key = msg.get("event")
        if isinstance(key, str) and _EVENT_KEY.match(key):
            return key
    func = getattr(record, "funcName", None)
    func = func if isinstance(func, str) and _LOGGER_NAME.match(func) else "unknown"
    return f"{_safe_logger(getattr(record, 'name', None))}.{func}"


def _safe_field(key: str, value: object) -> object | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value if math.isfinite(value) and abs(value) < NUMERIC_ABS_LIMIT else None
    if isinstance(value, str):
        if key.endswith(_HASH_SUFFIX):
            ok = (
                _HASH_VALUE.match(value)
                and re.search(r"[a-f]", value)
                and not _HASH_DIGIT_RUN.search(value)
            )
            return value if ok else None
        if _LONG_DIGITS.search(value):
            return None
        return value if _SAFE_VALUE.match(value) or _STATUS_VALUE.match(value) else None
    return None


def allowlisted_fields(record: Any) -> dict[str, object]:
    """Allowlisted structured fields from a structlog dict msg or stdlib `extra`."""
    msg = getattr(record, "msg", None)
    source: Any = msg if isinstance(msg, MutableMapping) else getattr(record, "__dict__", {})
    out: dict[str, object] = {}
    for key, value in list(source.items()):
        if not isinstance(key, str):
            continue
        if key not in ALLOWED_LOG_FIELDS and not key.endswith(_HASH_SUFFIX):
            continue
        safe = _safe_field(key, value)
        if safe is not None:
            out[key] = safe
    return out


def is_already_captured(record: Any) -> bool:
    msg = getattr(record, "msg", None)
    return isinstance(msg, MutableMapping) and bool(msg.get(CAPTURED_MARKER))


def minimal_log_event(ev: dict[str, Any], record: Any) -> dict[str, Any]:
    """Rebuild a message-only log event from code constants + allowlisted fields."""
    key = _event_key(record)
    fields = allowlisted_fields(record)
    error_class = next(
        (str(fields[f]) for f in _ERROR_CLASS_FIELDS if isinstance(fields.get(f), str)), "-"
    )
    out = {k: v for k, v in ev.items() if k in _EVENT_KEEP}
    out["logger"] = _safe_logger(getattr(record, "name", None))
    out["message"] = key
    out["fingerprint"] = [key, error_class]
    tags = dict(out.get("tags") or {})
    tags["log_event"] = key[:_TAG_MAX]
    out["tags"] = tags
    contexts = dict(out.get("contexts") or {})
    contexts["log_fields"] = fields
    out["contexts"] = contexts
    return out


_ERROR_METHODS = frozenset({"error", "exception", "critical", "fatal"})

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def _resolve_exc_info(value: object) -> _ExcInfo | None:
    if value is True:
        ei = sys.exc_info()
        return ei if ei[0] is not None and ei[1] is not None else None
    if isinstance(value, BaseException):
        return (type(value), value, value.__traceback__)
    if (
        isinstance(value, tuple)
        and len(value) == 3
        and isinstance(value[0], type)
        and isinstance(value[1], BaseException)
    ):
        return (value[0], value[1], value[2])
    return None


def structlog_capture_exc_info(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: send ``log.error(..., exc_info=e|True)`` /
    ``log.exception(...)`` to the SDK as an exception event.

    Place it BEFORE ``format_exc_info`` / ``dict_tracebacks`` (they consume
    ``exc_info``) and before ``wrap_for_formatter`` / the renderer. Only the
    exception is sent (scrubbed by scrub_event); the log kwargs are not. Tags:
    ``logger`` and ``log_event`` (the event key, only if it is a code constant).
    Never raises: logging must not break on an observability bug.
    """
    try:
        if method_name not in _ERROR_METHODS or event_dict.get(CAPTURED_MARKER):
            return event_dict
        exc_info = _resolve_exc_info(event_dict.get("exc_info"))
        if exc_info is None:
            return event_dict
        import sentry_sdk

        if not sentry_sdk.get_client().is_active():
            return event_dict
        name = event_dict.get("logger") or getattr(logger, "name", None)
        key = event_dict.get("event")
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("logger", _safe_logger(name))
            if isinstance(key, str) and _EVENT_KEY.match(key):
                scope.set_tag("log_event", key)
            sentry_sdk.capture_exception(exc_info)
        event_dict[CAPTURED_MARKER] = True
    except Exception:  # noqa: BLE001 - never break logging
        pass
    return event_dict
