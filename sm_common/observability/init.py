"""PHI-safe Sentry/GlitchTip initialisation. The ONLY place sentry_sdk.init is called.

Option choices, verified against sentry-sdk 2.71.0, are documented in the GlitchTip
Plan 2 "Decisions" table and Plan 4 (traces). sentry_sdk is imported lazily so that
sm_common works without the `observability` extra.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from sm_common.observability._controls import is_enabled
from sm_common.observability.scrub import scrub_breadcrumb, scrub_event
from sm_common.observability.traces import (
    make_traces_sampler,
    parse_propagation_targets,
    parse_sample_rate,
    scrub_transaction,
)

if TYPE_CHECKING:
    from sentry_sdk.transport import Transport

# Auto-enabling AI integrations: prompts are PII-gated today, but nothing they add to a
# span is worth the risk class. Disabled by name so a future send_default_pii flip or
# SDK default change cannot start shipping transcripts.
_DISABLED_INTEGRATIONS = (
    "sentry_sdk.integrations.openai.OpenAIIntegration",
    "sentry_sdk.integrations.openai_agents.OpenAIAgentsIntegration",
    "sentry_sdk.integrations.anthropic.AnthropicIntegration",
    "sentry_sdk.integrations.google_genai.GoogleGenAIIntegration",
    "sentry_sdk.integrations.langchain.LangchainIntegration",
    "sentry_sdk.integrations.langgraph.LanggraphIntegration",
    "sentry_sdk.integrations.huggingface_hub.HuggingfaceHubIntegration",
    "sentry_sdk.integrations.pydantic_ai.PydanticAIIntegration",
    "sentry_sdk.integrations.mcp.MCPIntegration",
)


def _disabled_integrations() -> list[Any]:
    import importlib

    out: list[Any] = []
    for path in _DISABLED_INTEGRATIONS:
        module, _, cls = path.rpartition(".")
        try:
            out.append(getattr(importlib.import_module(module), cls))
        except Exception:  # noqa: BLE001 - integration absent or its lib missing: nothing to disable
            continue
    return out


def _build_options(
    *,
    dsn: str,
    environment: str,
    release: str | None,
    server_name: str,
    transport: Transport | None,
    traces_sample_rate: float,
    trace_propagation_targets: list[str],
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
        "disabled_integrations": _disabled_integrations(),
        "enable_logs": False,
        "enable_metrics": False,
        "before_send": scrub_event,
        "before_breadcrumb": scrub_breadcrumb,
        "before_send_transaction": scrub_transaction,
        # Tracing does not gate header propagation: sentry-sdk 2.x ("tracing without
        # performance") adds sentry-trace + baggage to EVERY outbound
        # httpx/requests/urllib/aiohttp call by default (trace_propagation_targets
        # defaults to [".*"]), handing vendors (Gupshup, Exotel, Gemini...) our trace
        # ids and baggage. An empty
        # list matches no URL, which disables it in every HTTP integration
        # (tracing_utils.should_propagate_trace -> match_regex_list -> False).
        # v0.18.0: the list is [] unless the caller passes an explicit allowlist of OUR
        # OWN hosts, turned into anchored regexes by parse_propagation_targets.
    }
    if is_enabled("no_trace_propagation"):
        options["trace_propagation_targets"] = trace_propagation_targets
    if traces_sample_rate > 0:
        # traces_sampler, not traces_sample_rate: the SDK would otherwise follow an
        # incoming (forgeable) sentry-trace/baggage sampling decision past the ceiling.
        options["traces_sampler"] = make_traces_sampler(traces_sample_rate)
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
    traces_sample_rate: object = None,
    trace_propagation_targets: str | Iterable[str] | None = None,
) -> bool:
    """Errors at 100%. Traces only when ``traces_sample_rate`` (the raw TRACES_SAMPLE_RATE
    env value is accepted) parses to > 0; clamped to TRACES_SAMPLE_RATE_CEILING (0.25)
    and decided by our own coin. ``trace_propagation_targets`` is the raw
    SENTRY_TRACE_PROPAGATION_TARGETS value: comma-separated host names of our own
    services; default none, and anything that is not a plain host is ignored."""
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
            traces_sample_rate=parse_sample_rate(traces_sample_rate),
            trace_propagation_targets=parse_propagation_targets(trace_propagation_targets),
        )
    )
    sentry_sdk.get_global_scope().set_tag("service", service_name)
    return True
