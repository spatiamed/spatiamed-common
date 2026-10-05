"""before_send / before_breadcrumb scrubbers. Fail closed.

Order: log-event filter (V2) -> frame vars (V1) -> structural deletions (V3/V7)
-> one recursive walk over the WHOLE event (sensitive keys -> [Filtered], every
string through redact_text) (V3/V4/V5/V8 backstop).

Any internal error returns None, so the event is dropped. sentry-sdk also drops the
event if before_send raises (client.py:926-950 in 2.71.0). An unscrubbed event is
never returned. Does not import sentry_sdk at runtime.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from sm_common.observability._controls import is_enabled
from sm_common.observability.pii import redact_text

if TYPE_CHECKING:
    from sentry_sdk._types import Breadcrumb, BreadcrumbHint, Event, Hint

FILTERED = "[Filtered]"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "session",
    "signature",
    "otp",
    "phone",
    "mobile",
    "email",
    "aadhaar",
    "abha",
    "address",
    "dob",
    "patient_name",
    "first_name",
    "last_name",
    "full_name",
    "query",
    "bearer",
    "jwt",
    "credential",
)
_TOKEN_KEY = re.compile(r"(^|[_\-.])token$")
_HEADER_ALLOWLIST = frozenset(
    {"host", "user-agent", "content-type", "content-length", "accept", "x-request-id"}
)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(part in k for part in _SENSITIVE_KEY_PARTS) or bool(_TOKEN_KEY.search(k))


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            sensitive = isinstance(k, str) and _is_sensitive_key(k) and v not in (None, "")
            out[k] = FILTERED if sensitive else _walk(v)
        return out
    if isinstance(obj, list):
        return [_walk(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v) for v in obj)
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def _frames(ev: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for section in ("exception", "threads"):
        for value in (ev.get(section) or {}).get("values") or []:
            out.extend(((value or {}).get("stacktrace") or {}).get("frames") or [])
    return out


def _structural(ev: dict[str, Any]) -> None:
    req = ev.get("request")
    if isinstance(req, dict):
        kept: dict[str, Any] = {}
        if "method" in req:
            kept["method"] = req["method"]
        if isinstance(req.get("url"), str):
            kept["url"] = req["url"].split("?", 1)[0].split("#", 1)[0]
        headers = req.get("headers")
        if isinstance(headers, dict):
            kept["headers"] = {k: v for k, v in headers.items() if k.lower() in _HEADER_ALLOWLIST}
        ev["request"] = kept
    ev.pop("extra", None)
    user = ev.get("user")
    if isinstance(user, dict):
        ev["user"] = {"id": user["id"]} if "id" in user else {}


def scrub_event(event: Event, hint: Hint) -> Event | None:
    try:
        ev = cast(dict[str, Any], event)
        if is_enabled("log_events") and "log_record" in (hint or {}):
            if not ev.get("exception"):
                return None
            ev.pop("logentry", None)
            ev.pop("extra", None)
        if is_enabled("frames_vars"):
            for frame in _frames(ev):
                frame.pop("vars", None)
        if is_enabled("structural"):
            _structural(ev)
        if is_enabled("walk"):
            ev = cast(dict[str, Any], _walk(ev))
        return cast("Event", ev)
    except Exception:  # noqa: BLE001 - fail closed: drop, never send unscrubbed
        return None


def scrub_breadcrumb(crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb | None:
    try:
        if is_enabled("walk"):
            return cast("Breadcrumb", _walk(crumb))
        return crumb
    except Exception:  # noqa: BLE001 - fail closed
        return None
