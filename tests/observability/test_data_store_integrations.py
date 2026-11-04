"""Opt-in: init_error_tracking(disable_data_store_integrations=True) (QueueCare perf 2026-11-05).

The SQLAlchemy, asyncpg and Redis integrations record a breadcrumb per statement and
command; scrubbing them was ~45% of a single-process API's per-request CPU. Off, errors
are still captured and scrubbed the same way. Default stays on (no consumer changes
until it opts in).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sentry_sdk
import sqlalchemy as sa

from sm_common.observability import init_error_tracking
from tests.observability.conftest import DUMMY_DSN, MemoryTransport, fake_phone, nonce


@pytest.fixture
def init() -> Iterator[object]:
    def _init(**kwargs: object) -> MemoryTransport:
        t = MemoryTransport()
        init_error_tracking(
            dsn=DUMMY_DSN,
            environment="prod",
            release="sha-test",
            service_name="unit",
            transport=t,
            **kwargs,
        )
        return t

    yield _init
    sentry_sdk.get_global_scope().set_client(None)


def _query_and_fail(t: MemoryTransport) -> dict:
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT 1"))
    sentry_sdk.capture_exception(RuntimeError(f"DSI{nonce()}"))
    sentry_sdk.flush()
    sentry_sdk.get_isolation_scope().clear_breadcrumbs()
    return t.only_event()


def _query_crumbs(event: dict) -> list:
    return [
        c for c in event.get("breadcrumbs", {}).get("values", []) if c.get("category") == "query"
    ]


def test_default_keeps_data_store_integrations(init) -> None:  # type: ignore[no-untyped-def]
    t = init()
    active = set(sentry_sdk.get_client().integrations)
    assert {"sqlalchemy", "redis"} <= active
    assert _query_crumbs(_query_and_fail(t)), "default must still record SQL breadcrumbs"


# sentry_sdk installs an integration once per PROCESS (integrations/__init__.py
# _installed_integrations): once any earlier init in this pytest process enabled
# SQLAlchemy, a later init cannot un-patch it. Services init exactly once at import,
# so the contract is checked in a fresh interpreter, the way they run.
_FRESH_PROCESS_CHECK = """
import json, sentry_sdk, sqlalchemy as sa
from sm_common.observability import init_error_tracking
from tests.observability.conftest import DUMMY_DSN, MemoryTransport
t = MemoryTransport()
init_error_tracking(dsn=DUMMY_DSN, environment="prod", release="sha-test", service_name="unit",
                    transport=t, disable_data_store_integrations=True)
engine = sa.create_engine("sqlite://")
with engine.connect() as conn:
    conn.execute(sa.text("SELECT 1"))
sentry_sdk.capture_exception(RuntimeError("DSI-fresh"))
sentry_sdk.flush()
values = t.only_event().get("breadcrumbs", {}).get("values", [])
crumbs = [c for c in values if c.get("category") == "query"]
active = sorted(sentry_sdk.get_client().integrations)
print(json.dumps({"active": active, "query_crumbs": len(crumbs)}))
"""


def test_opt_in_disables_sqlalchemy_asyncpg_redis() -> None:
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, "-c", _FRESH_PROCESS_CHECK],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert set(result["active"]).isdisjoint({"sqlalchemy", "asyncpg", "redis"}), result["active"]
    assert "logging" in result["active"], "the rest of the SDK must still be on"
    assert result["query_crumbs"] == 0


def test_opt_in_keeps_every_other_safety_option(init) -> None:  # type: ignore[no-untyped-def]
    init(disable_data_store_integrations=True)
    opts = sentry_sdk.get_client().options
    assert opts["send_default_pii"] is False
    assert opts["include_local_variables"] is False
    assert opts["max_request_body_size"] == "never"
    assert opts["before_send"].__name__ == "scrub_event"
    assert opts["before_breadcrumb"].__name__ == "scrub_breadcrumb"
    # Every integration the default turns off (AI SDKs, loguru) stays off.
    from sm_common.observability.init import _disabled_integrations

    assert set(_disabled_integrations(False)) <= set(opts["disabled_integrations"])
    assert set(_disabled_integrations(False)) < set(_disabled_integrations(True))


def test_opt_in_error_event_still_scrubbed(init) -> None:  # type: ignore[no-untyped-def]
    t = init(disable_data_store_integrations=True)
    phone = fake_phone()
    marker = f"DSS{nonce()}"
    sentry_sdk.capture_exception(RuntimeError(f"{marker} caller {phone} failed"))
    sentry_sdk.flush()
    blob = t.blob()
    assert marker in blob, "the error event must still be captured"
    assert phone not in blob, "the existing scrubber must still redact the phone"
