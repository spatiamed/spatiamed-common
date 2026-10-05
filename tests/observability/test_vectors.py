from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.observability.conftest import (
    Capture,
    fake_aadhaar,
    fake_email,
    fake_jwt,
    fake_name,
    fake_phone,
    nonce,
)


def _raise_in(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        sentry_sdk.capture_exception()


# V1 frame locals
def case_v1_frame_locals(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    name, marker = fake_name(), f"GTV1{nonce()}"

    def handler(patient_name: str) -> None:
        local_copy = patient_name  # noqa: F841
        raise RuntimeError(marker)

    with capture(disable) as t:
        _raise_in(handler, name)
    assert marker in t.blob()
    assert name not in t.blob()


# V2a logger.error without exc_info never becomes an event
def case_v2_log_error_dropped(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    name = fake_name()
    with capture(disable) as t:
        logging.getLogger("gt.v2").error("intake failed for %s", name)
    assert t.events == []


# V2b logger.exception keeps the exception, loses logentry/extra
def case_v2_logged_exception(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    name, marker = fake_name(), f"GTV2{nonce()}"
    with capture(disable) as t:
        try:
            raise RuntimeError(marker)
        except RuntimeError:
            logging.getLogger("gt.v2").exception(
                "booking failed for %s", name, extra={"complaint": name}
            )
    assert marker in t.blob()
    assert name not in t.blob()


# V2c structlog kwargs (PA/CL style wrap_for_formatter) never reach a breadcrumb
def case_v2_structlog_breadcrumb(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    name, marker = fake_name(), f"GTV2S{nonce()}"
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    try:
        with capture(disable) as t:
            structlog.get_logger("gt.v2s").error("intake_failed", patient_name=name)
            _raise_in(lambda: (_ for _ in ()).throw(RuntimeError(marker)))
        assert marker in t.blob()
        assert name not in t.blob()
    finally:
        structlog.reset_defaults()


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/search")
    def search() -> None:
        raise RuntimeError(f"GTV3{nonce()}")

    @app.get("/t/{capability}/status")
    def status(capability: str) -> None:
        raise RuntimeError(f"GTV4{nonce()}")

    @app.post("/intake")
    async def intake(body: dict[str, Any]) -> None:
        raise RuntimeError(f"GTV7{nonce()}")

    return app


# V3 request url + query string
def case_v3_query_string(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    phone, name = fake_phone(), fake_name()
    with capture(disable) as t:
        TestClient(_app(), raise_server_exceptions=False).get(f"/search?phone={phone}&q={name}")
    ev = t.only_event()
    assert "GTV3" in t.blob()
    assert phone not in t.blob() and name.split()[0] not in t.blob()
    assert "?" not in ev["request"]["url"]


# V4 /t/<capability>
def case_v4_capability(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    cap = f"cap{nonce()}{nonce()}"
    with capture(disable) as t:
        TestClient(_app(), raise_server_exceptions=False).get(f"/t/{cap}/status")
    assert "GTV4" in t.blob()
    assert cap not in t.blob()


# V5 outbound httpx URL breadcrumb (Exotel/Gupshup style)
def case_v5_breadcrumb_data(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    phone, phone2, name, marker = fake_phone(), fake_phone(), fake_name(), f"GTV5{nonce()}"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with capture(disable) as t:
        client.get(f"https://api.exotel.com/v1/Calls/{phone}", params={"To": phone2, "Body": name})
        _raise_in(lambda: (_ for _ in ()).throw(RuntimeError(marker)))
    ev = t.only_event()
    assert any(c.get("category") == "httplib" for c in ev.get("breadcrumbs", {}).get("values", []))
    for s in (phone, phone2, name.split()[0]):
        assert s not in t.blob()


# V7 bodies, cookies, headers
def case_v7_bodies_cookies_headers(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    name, jwt, cookie, key = fake_name(), fake_jwt(), f"ck{nonce()}", f"ak{nonce()}"
    with capture(disable) as t:
        TestClient(_app(), raise_server_exceptions=False).post(
            "/intake",
            json={"complaint": f"{name} has chest pain"},
            headers={
                "Authorization": f"Bearer {jwt}",
                "Cookie": f"sm_session={cookie}",
                "X-Api-Key": key,
                "X-Forwarded-For": "203.0.113.77",
                "X-Request-Id": f"rid{nonce()}",
            },
        )
    blob = t.blob()
    assert "GTV7" in blob
    for s in (name.split()[0], jwt, cookie, key, "203.0.113.77"):
        assert s not in blob
    assert (
        "X-Request-Id" in str(t.only_event()["request"].get("headers", {}))
        or "x-request-id" in blob
    )


# V8 identifiers-not-PHI backstop in the exception message (= issue title)
def case_v8_exception_message(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    phone, email, aadhaar, jwt, marker = (
        fake_phone(),
        fake_email(),
        fake_aadhaar(),
        fake_jwt(),
        f"GTV8{nonce()}",
    )
    with capture(disable) as t:
        _raise_in(
            lambda: (_ for _ in ()).throw(
                ValueError(
                    f"lookup failed phone={phone} email={email} "
                    f"aadhaar={aadhaar} jwt={jwt} {marker}"
                )
            )
        )
    value = t.only_event()["exception"]["values"][-1]["value"]
    assert marker in value
    for s in (phone, email, aadhaar, jwt):
        assert s not in t.blob()


VECTORS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "V1": (case_v1_frame_locals, ("include_local_variables", "frames_vars")),
    "V2a": (case_v2_log_error_dropped, ("log_events",)),
    "V2b": (case_v2_logged_exception, ("log_events",)),
    "V2c": (case_v2_structlog_breadcrumb, ("logging_breadcrumbs_off",)),
    "V3": (case_v3_query_string, ("structural", "walk")),
    "V4": (case_v4_capability, ("walk",)),
    "V5": (case_v5_breadcrumb_data, ("walk",)),
    "V7": (case_v7_bodies_cookies_headers, ("max_request_body_size", "structural", "walk")),
    "V8": (case_v8_exception_message, ("walk",)),
}


@pytest.mark.parametrize("vector", sorted(VECTORS))
def test_vector_is_scrubbed(vector: str, capture: Capture) -> None:
    fn, _ = VECTORS[vector]
    fn(capture)


@pytest.mark.parametrize("vector", sorted(VECTORS))
def test_vector_test_fails_without_its_defence(vector: str, capture: Capture) -> None:
    """Mutation check: with the vector's controls off, its test must fail."""
    fn, controls = VECTORS[vector]
    with pytest.raises(AssertionError):
        fn(capture, disable=controls)


@pytest.mark.xfail(
    strict=True,
    reason="Names cannot be regex-redacted (spec §6.5/§9 residual); D5 contract is the control",
)
def test_v8_bare_name_in_exception_message_is_not_redactable(capture: Capture) -> None:
    name = fake_name()
    with capture() as t:
        _raise_in(lambda: (_ for _ in ()).throw(ValueError(f"Patient {name} not found")))
    assert name not in t.blob()


def test_scrubber_crash_drops_event(capture: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    from sm_common.observability import scrub

    monkeypatch.setattr(scrub, "_walk", lambda _: (_ for _ in ()).throw(RuntimeError("bug")))
    with capture() as t:
        _raise_in(lambda: (_ for _ in ()).throw(RuntimeError(f"GTX{nonce()}")))
    assert t.events == []
