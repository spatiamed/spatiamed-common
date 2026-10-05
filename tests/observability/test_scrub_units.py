from typing import Any, cast

import pytest

from sm_common.observability import _controls
from sm_common.observability.scrub import FILTERED, scrub_breadcrumb, scrub_event


def _ev(**kw: Any) -> Any:
    return cast(Any, kw)


def test_frames_vars_deleted_everywhere() -> None:
    frame = {"function": "f", "vars": {"name": "x"}}
    ev = _ev(
        exception={
            "values": [{"type": "E", "value": "v", "stacktrace": {"frames": [dict(frame)]}}]
        },
        threads={"values": [{"stacktrace": {"frames": [dict(frame)]}}]},
    )
    out = scrub_event(ev, {})
    assert out is not None
    assert "vars" not in out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert "vars" not in out["threads"]["values"][0]["stacktrace"]["frames"][0]


def test_log_event_without_exception_is_dropped() -> None:
    assert scrub_event(_ev(logentry={"message": "m"}), {"log_record": object()}) is None


def test_log_event_with_exception_loses_logentry_and_extra() -> None:
    ev = _ev(
        logentry={"message": "Zed", "params": []},
        extra={"k": "Zed"},
        exception={"values": [{"type": "E", "value": "ok"}]},
    )
    out = scrub_event(ev, {"log_record": object()})
    assert out is not None and "logentry" not in out and "extra" not in out


def test_structural_request_allowlist() -> None:
    ev = _ev(
        request={
            "method": "POST",
            "url": "https://h/x?phone=1",
            "query_string": "phone=1",
            "data": {"a": 1},
            "cookies": {"s": "1"},
            "env": {"REMOTE_ADDR": "1.2.3.4"},
            "headers": {
                "User-Agent": "ua",
                "X-Request-Id": "rid",
                "Authorization": "Bearer x",
                "Cookie": "s=1",
                "X-Forwarded-For": "1.2.3.4",
                "X-Api-Key": "k",
            },
        },
        user={"id": "u1", "email": "a@b.co", "ip_address": "1.2.3.4"},
        extra={"x": 1},
    )
    out = scrub_event(ev, {})
    assert out is not None
    assert out["request"] == {
        "method": "POST",
        "url": "https://h/x",
        "headers": {"User-Agent": "ua", "X-Request-Id": "rid"},
    }
    assert out["user"] == {"id": "u1"}
    assert "extra" not in out


def test_walk_filters_sensitive_keys_and_redacts_strings() -> None:
    ev = _ev(
        tags={"phone": "x", "token_number": "42"},
        contexts={"c": {"access_token": "t", "note": "call 9876543210"}},
        message="mail a@b.co",
    )
    out = scrub_event(ev, {})
    assert out is not None
    assert out["tags"] == {"phone": FILTERED, "token_number": "42"}
    assert out["contexts"]["c"] == {"access_token": FILTERED, "note": "call [PHONE]"}
    assert out["message"] == "mail [EMAIL]"


def test_ids_and_timestamps_survive_the_walk() -> None:
    eid = "a1b2c3d46789012345e6f7a8b9c0d1e2"
    out = scrub_event(_ev(event_id=eid, timestamp="2026-10-06T12:00:00Z", message="x"), {})
    assert out is not None
    assert out["event_id"] == eid and out["timestamp"] == "2026-10-06T12:00:00Z"


def test_breadcrumb_walked_including_data() -> None:
    crumb = cast(
        Any,
        {"message": "", "data": {"url": "https://x/Calls/9876543210?To=1", "http.query": "To=1"}},
    )
    out = scrub_breadcrumb(crumb, {})
    assert out is not None
    assert out["data"] == {"url": "https://x/Calls/[PHONE]?[Filtered]", "http.query": FILTERED}


def test_internal_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from sm_common.observability import scrub

    def boom(_: object) -> object:
        raise RuntimeError("bug")

    monkeypatch.setattr(scrub, "_walk", boom)
    assert scrub_event(_ev(message="x"), {}) is None
    assert scrub_breadcrumb(cast(Any, {"message": "x"}), {}) is None


def test_disabled_rejects_unknown_control() -> None:
    with pytest.raises(ValueError), _controls.disabled("nope"):
        pass
