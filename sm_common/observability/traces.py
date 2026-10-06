"""PHI-safe performance traces: sample-rate parsing, propagation targets, sampler and
the before_send_transaction scrubber. Fail closed. No sentry_sdk import at runtime.

Verified against sentry-sdk 2.71.0 (GlitchTip Plan 4). With send_default_pii=False the
SDK STILL puts these on a transaction: request.query_string and the raw request.url
(asgi), the URL as the transaction name for an unmatched route (source "url"), SQL
literals in db span descriptions (sqlalchemy + asyncpg), the full outbound URL in
http.client descriptions plus data url/http.query, and the redis key in descriptions,
tags (redis.key) and data (redis.commands). This module removes every one of them.
"""

from __future__ import annotations

import logging
import math
import random
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from sm_common.observability._controls import is_enabled
from sm_common.observability.pii import redact_text

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

log = logging.getLogger(__name__)

TRACES_SAMPLE_RATE_CEILING = 0.25
UNMATCHED_TRANSACTION = "<unmatched route>"
ID = "{id}"
_MAX_DESCRIPTION = 500

# Transaction-name sources that are templates (safe to keep after redact_text).
_TEMPLATE_SOURCES = frozenset({"route", "component", "view", "task", "custom"})
# Span data / tag keys that may leave the process. Everything else is dropped.
_SPAN_DATA_ALLOW = frozenset(
    {
        "db.system",
        "db.operation",
        "db.name",
        "db.driver.name",
        "server.port",
        "http.method",
        "http.request.method",
        "http.response.status_code",
        "http.status_code",
        "messaging.system",
        "messaging.destination.name",
        "messaging.message.retry.count",
        "sentry.origin",
        "sentry.op",
        "thread.name",
    }
)
_SPAN_TAG_ALLOW = frozenset(
    {"http.status_code", "status", "redis.command", "db.operation", "redis.is_cluster"}
)
# Probe paths are never sampled: on staging 2026-10-06, 71,209 of 71,855 requests in
# 24 h (99%) were /health or /health/ready (Caddy logs). Matches any path SEGMENT, so
# /api/kiosk/health is covered too.
_PROBE_PATH = re.compile(r"(?:^|/)(?:health|healthz|ready|readyz|livez|metrics)(?:/|$)")

_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)
_SQL_DOLLAR = re.compile(r"\$(\w*)\$.*?\$\1\$", re.S)
_SQL_ESTRING = re.compile(r"(?<![\w])[Ee]'(?:[^'\\]|\\.|'')*'")
_SQL_STRING = re.compile(r"(?:(?<![\w])[BbXxNnUu]&?)?'(?:[^']|'')*'")
_SQL_NUMBER = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])")
_SQL_IN_LIST = re.compile(r"(?i)\bIN\s*\(\s*\?(?:\s*,\s*\?)+\s*\)")
_URL = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)?(?:[^/@?#]*@)?(?P<rest>[^?#]*)")
# A path segment that is only digits (4+): record ids, phone numbers without a + etc.
_DIGIT_SEGMENT = re.compile(r"/\d{4,}(?=/|$)")


def parse_sample_rate(raw: object) -> float:
    """Env value -> rate in [0, CEILING]. Anything unparseable, negative or NaN is 0 (off)."""
    if raw is None or raw == "":
        return 0.0
    try:
        rate = float(cast(Any, raw))
    except (TypeError, ValueError):
        log.warning("traces sample rate unparseable; tracing OFF")
        return 0.0
    if math.isnan(rate) or rate <= 0:
        return 0.0
    if rate > TRACES_SAMPLE_RATE_CEILING:
        log.warning("traces sample rate %.3f above ceiling; clamped", rate)
        return TRACES_SAMPLE_RATE_CEILING
    return rate


def parse_propagation_targets(raw: str | Iterable[str] | None) -> list[str]:
    """Comma-separated hosts or origins (e.g. "api.staging.spatiamed.com,https://qc.x.com")
    -> anchored regexes. Default: [] = propagate to NOTHING (the SDK default is ".*", every
    vendor). Compose names (queuecare-server:8000) are hosts too. Anything that is not a
    plain host ("*", ".*", a regex, a path) is DROPPED with a
    warning, never raised: a typo in an env var must not stop the service from booting."""
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in (i.strip() for i in items):
        if not item:
            continue
        host = re.sub(r"^https?://", "", item).rstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?", host):
            log.warning("ignoring trace propagation target that is not a host name")
            continue
        out.append(rf"^https?://{re.escape(host)}(?:[/?#]|$)")
    return out


def make_traces_sampler(rate: float) -> Callable[[dict[str, Any]], bool]:
    """Own coin flip, so a forged incoming sentry-trace / baggage (sampled=1,
    sample_rand=0) cannot force sampling past the ceiling. Celery tasks follow their
    parent (our own producer set it). Websockets and health probes are never sampled."""
    rate = parse_sample_rate(rate)

    def sampler(ctx: dict[str, Any]) -> bool:
        if rate <= 0:
            return False
        scope = ctx.get("asgi_scope") or {}
        if scope.get("type") == "websocket" or _PROBE_PATH.search(str(scope.get("path", ""))):
            return False
        if "celery_job" in ctx and ctx.get("parent_sampled") is not None:
            return bool(ctx["parent_sampled"])
        return random.random() < rate

    return sampler


def strip_sql_literals(sql: str) -> str:
    """Keep the statement shape, drop every literal: strings, dollar-quoted bodies,
    numbers, comments. Placeholders ($1, %(x)s, :name, ?) are untouched."""
    out = _SQL_COMMENT.sub(" ", sql)
    out = _SQL_DOLLAR.sub("?", out)
    out = _SQL_ESTRING.sub("?", out)
    out = _SQL_STRING.sub("?", out)
    out = _SQL_NUMBER.sub("?", out)
    out = _SQL_IN_LIST.sub("IN (?)", out)
    return re.sub(r"\s+", " ", out).strip()[:_MAX_DESCRIPTION]


def scrub_url(url: str) -> str:
    """scheme://host/path with no userinfo, query or fragment; /t/<cap>, phones,
    emails, Aadhaar via redact_text; then any 4+ digit path segment -> {id}."""
    m = _URL.match(url)
    base = (m.group("scheme") or "") + m.group("rest") if m else url.split("?", 1)[0]
    return _DIGIT_SEGMENT.sub("/" + ID, redact_text(base.split("#", 1)[0]))


def _http_description(desc: str) -> str:
    method, _, rest = desc.partition(" ")
    if rest and method.isupper():
        return f"{method} {scrub_url(rest)}"
    return scrub_url(desc)


def _span_description(op: str, desc: str) -> str:
    if is_enabled("span_sql") and op.startswith("db") and not op.startswith("db.redis"):
        return strip_sql_literals(desc)
    if is_enabled("span_redis") and (op.startswith("db.redis") or op.startswith("cache")):
        return desc.split(" ", 1)[0][:64]  # the command only; never the key or args
    if is_enabled("span_http") and op.startswith("http"):
        return _http_description(desc)
    if is_enabled("span_subprocess") and op.startswith("subprocess"):
        first = desc.split(" ", 1)[0]
        return first.rsplit("/", 1)[-1][:64]
    return redact_text(desc)[:_MAX_DESCRIPTION] if is_enabled("span_text") else desc


def _scrub_span(span: dict[str, Any]) -> dict[str, Any]:
    op = str(span.get("op") or "")
    if isinstance(span.get("description"), str):
        span["description"] = _span_description(op, span["description"])
    if is_enabled("span_data_allowlist"):
        for key in ("data", "tags"):
            allow = _SPAN_DATA_ALLOW if key == "data" else _SPAN_TAG_ALLOW
            value = span.get(key)
            if isinstance(value, dict):
                span[key] = {k: v for k, v in value.items() if k in allow}
    return span


def _transaction_name(ev: dict[str, Any]) -> None:
    source = ((ev.get("transaction_info") or {}).get("source")) or ""
    name = ev.get("transaction")
    if not isinstance(name, str):
        return
    if is_enabled("transaction_name") and source not in _TEMPLATE_SOURCES:
        ev["transaction"] = UNMATCHED_TRANSACTION
        ev["transaction_info"] = {"source": "custom"}
    else:
        ev["transaction"] = (
            redact_text(name)[:_MAX_DESCRIPTION] if is_enabled("span_text") else name
        )


def scrub_transaction(event: Event, hint: Hint) -> Event | None:
    """before_send_transaction. Any internal error drops the transaction."""
    from sm_common.observability.scrub import _structural, _walk

    try:
        ev = cast(dict[str, Any], event)
        _transaction_name(ev)
        if is_enabled("structural"):
            _structural(ev)
            # A transaction needs only the method (GlitchTip groups on name/op/method).
            # The url carries path params (/patients/by-name/{name}) even on a matched route.
            method = (ev.get("request") or {}).get("method")
            ev["request"] = {"method": method} if method else {}
        trace = (ev.get("contexts") or {}).get("trace")
        if isinstance(trace, dict):
            _scrub_span(trace)
        ev["spans"] = [_scrub_span(s) for s in ev.get("spans") or [] if isinstance(s, dict)]
        if is_enabled("walk"):
            ev = cast(dict[str, Any], _walk(ev))
        return cast("Event", ev)
    except Exception:  # noqa: BLE001 - fail closed
        return None
