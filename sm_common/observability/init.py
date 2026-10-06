"""PHI-safe Sentry/GlitchTip initialisation. The ONLY place sentry_sdk.init is called.

Option choices, verified against sentry-sdk 2.71.0, are documented in the GlitchTip
Plan 2 "Decisions" table. sentry_sdk is imported lazily so that sm_common works
without the `observability` extra.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sm_common.observability._controls import is_enabled
from sm_common.observability.scrub import scrub_breadcrumb, scrub_event

if TYPE_CHECKING:
    from sentry_sdk.transport import Transport


def _build_options(
    *,
    dsn: str,
    environment: str,
    release: str | None,
    server_name: str,
    transport: Transport | None,
) -> dict[str, Any]:
    from sentry_sdk.integrations.logging import LoggingIntegration

    options: dict[str, Any] = {
        "dsn": dsn,
        "environment": environment,
        "release": release if release and release != "unknown" else None,
        # Explicit service identity. The SDK default is socket.gethostname()
        # (client.py:336-337), which leaks a laptop/container host name.
        "server_name": server_name if is_enabled("explicit_server_name") else None,
        "send_default_pii": False,
        "include_local_variables": not is_enabled("include_local_variables"),
        "max_request_body_size": "never" if is_enabled("max_request_body_size") else "medium",
        "integrations": [
            LoggingIntegration(
                level=None if is_enabled("logging_breadcrumbs_off") else logging.INFO,
                event_level=logging.ERROR,
                sentry_logs_level=None,
            )
        ],
        "enable_logs": False,
        "enable_metrics": False,
        "before_send": scrub_event,
        "before_breadcrumb": scrub_breadcrumb,
        # traces_sample_rate is omitted, so no transactions/spans are sent. That does
        # NOT stop header propagation: sentry-sdk 2.x ("tracing without performance")
        # adds sentry-trace + baggage to EVERY outbound httpx/requests/urllib/aiohttp
        # call by default (trace_propagation_targets defaults to [".*"]), handing
        # vendors (Gupshup, Exotel, Gemini...) our trace ids and baggage. An empty
        # list matches no URL, which disables it in every HTTP integration
        # (tracing_utils.should_propagate_trace -> match_regex_list -> False).
    }
    if is_enabled("no_trace_propagation"):
        options["trace_propagation_targets"] = []
    if transport is not None:
        options["transport"] = transport
    return options


def init_error_tracking(
    *,
    dsn: str | None,
    environment: str,
    release: str | None,
    service_name: str,
    server_name: str | None = None,
    transport: Transport | None = None,
) -> bool:
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        **_build_options(
            dsn=dsn,
            environment=environment,
            release=release,
            server_name=server_name or f"{service_name}-{environment}",
            transport=transport,
        )
    )
    sentry_sdk.get_global_scope().set_tag("service", service_name)
    return True
