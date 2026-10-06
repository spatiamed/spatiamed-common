"""Named safety controls. TEST-ONLY switchboard.

Each PHI defence (an SDK option or a scrub step) has a name. The vector tests
switch controls off to prove each test fails without its defence. Application
code must never import this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

CONTROLS: frozenset[str] = frozenset(
    {
        "include_local_variables",  # SDK option (V1)
        "max_request_body_size",  # SDK option (V7)
        "logging_breadcrumbs_off",  # SDK option (V2)
        "log_events",  # scrub step (V2)
        "frames_vars",  # scrub step (V1)
        "structural",  # scrub step (V3, V7)
        "walk",  # scrub step (V3, V4, V5, V8)
        "explicit_server_name",  # SDK option: never the host name
        "no_trace_propagation",  # SDK option: no sentry-trace/baggage except own hosts (T6)
        # Plan 4 (traces)
        "transaction_name",  # scrub step: URL-sourced names -> constant (T1)
        "span_sql",  # scrub step: SQL literals stripped (T2, T8)
        "span_http",  # scrub step: outbound URL path/query/fragment (T3)
        "span_redis",  # scrub step: redis key + args (T4)
        "span_subprocess",  # scrub step: subprocess argv -> executable basename (T9)
        "span_data_allowlist",  # scrub step: span data/tags allowlist (T3, T4)
        "span_text",  # scrub step: redact_text on names/descriptions (T7)
    }
)

_disabled: set[str] = set()


def is_enabled(name: str) -> bool:
    return name not in _disabled


@contextmanager
def disabled(*names: str) -> Iterator[None]:
    unknown = set(names) - CONTROLS
    if unknown:
        raise ValueError(f"unknown controls: {sorted(unknown)}")
    _disabled.update(names)
    try:
        yield
    finally:
        _disabled.difference_update(names)
