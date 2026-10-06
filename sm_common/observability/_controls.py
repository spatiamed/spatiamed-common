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
        "no_trace_propagation",  # SDK option: no sentry-trace/baggage on outbound calls
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
