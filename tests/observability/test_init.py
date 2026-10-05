import socket

import pytest
import sentry_sdk

from sm_common.observability import init_error_tracking
from tests.observability.conftest import Capture, nonce


def test_no_dsn_is_a_noop() -> None:
    sentry_sdk.get_global_scope().set_client(None)
    assert init_error_tracking(dsn=None, environment="prod", release="x", service_name="s") is False
    assert init_error_tracking(dsn="", environment="prod", release="x", service_name="s") is False
    assert sentry_sdk.get_client().is_active() is False


def test_environment_release_service_and_posture(capture: Capture) -> None:
    with capture() as t:
        sentry_sdk.capture_exception(RuntimeError(f"GTI{nonce()}"))
    ev = t.only_event()
    assert ev["environment"] == "prod" and ev["release"] == "sha-test"
    assert ev["tags"]["service"] == "unit"
    opts = sentry_sdk.get_client().options
    assert opts["send_default_pii"] is False
    assert opts["include_local_variables"] is False
    assert opts["max_request_body_size"] == "never"
    assert opts["traces_sample_rate"] is None
    assert opts["enable_logs"] is False and opts["enable_metrics"] is False


def test_unknown_release_is_omitted() -> None:
    from tests.observability.conftest import DUMMY_DSN, MemoryTransport

    t = MemoryTransport()
    init_error_tracking(
        dsn=DUMMY_DSN, environment="dev", release="unknown", service_name="s", transport=t
    )
    sentry_sdk.capture_exception(RuntimeError(f"GTR{nonce()}"))
    # "unknown" -> None -> the SDK auto-detects (client.py:327-328 get_default_release:
    # SENTRY_RELEASE env, then `git rev-parse HEAD` in cwd). Inside this checkout that is
    # sm_common's HEAD; in containers (no .git) it stays empty. Accepted. The contract:
    assert t.only_event().get("release") != "unknown"
    sentry_sdk.get_global_scope().set_client(None)


HOST_SENTINEL = "akjha-laptop.local"


def _case_server_name(
    capture: Capture, monkeypatch: pytest.MonkeyPatch, disable: tuple[str, ...] = ()
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: HOST_SENTINEL)
    with capture(disable) as t:
        sentry_sdk.capture_exception(RuntimeError(f"GTS{nonce()}"))
    ev = t.only_event()
    assert ev["server_name"] == "unit-prod"
    assert ev["server_name"] != socket.gethostname()
    assert HOST_SENTINEL not in t.blob()


def test_server_name_is_service_identity_never_hostname(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case_server_name(capture, monkeypatch)


def test_server_name_test_fails_without_explicit_server_name(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AssertionError):
        _case_server_name(capture, monkeypatch, disable=("explicit_server_name",))


def test_explicit_server_name_kwarg_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.observability.conftest import DUMMY_DSN, MemoryTransport

    monkeypatch.setattr(socket, "gethostname", lambda: HOST_SENTINEL)
    t = MemoryTransport()
    init_error_tracking(
        dsn=DUMMY_DSN,
        environment="staging",
        release="x",
        service_name="careloop",
        server_name="careloop-api-staging",
        transport=t,
    )
    sentry_sdk.capture_exception(RuntimeError(f"GTK{nonce()}"))
    sentry_sdk.flush()
    sentry_sdk.get_global_scope().set_client(None)
    assert t.only_event()["server_name"] == "careloop-api-staging"
    assert HOST_SENTINEL not in t.blob()
