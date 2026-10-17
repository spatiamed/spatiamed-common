"""Message-only log events (minimal, allowlisted, rate limited) and the structlog
exc_info processor. Each PHI test has a mutation twin that must fail with the
control off."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
import structlog

from sm_common.observability import logcapture, structlog_capture_exc_info
from tests.observability.conftest import Capture, fake_name, nonce


def _configure_structlog(*, with_processor: bool) -> None:
    """CareLoop / platform-api style: stdlib LoggerFactory + wrap_for_formatter."""
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
    ]
    if with_processor:
        processors.append(structlog_capture_exc_info)
    processors += [
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def slog() -> Iterator[None]:
    _configure_structlog(with_processor=True)
    yield
    structlog.reset_defaults()


# V2e structlog message-only error: event key + allowlisted fields ship, nothing else.
def case_structlog_message_only(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    name = fake_name()
    call_hash = "ab12cd34ef56"
    with capture(disable) as t:
        structlog.get_logger("app.realtime").error(
            "realtime_pipeline_error",
            provider="sarvam",
            error_type="SarvamCreditsExhausted",
            status_code=402,
            fatal=True,
            call_id_hash=call_hash,
            # NOT allowlisted (and not caught by the sensitive-key walk): must drop.
            complaint=f"{name} has chest pain",
            stage=f"{name} free text",  # allowlisted key, non-identifier value: drop
        )
    ev = t.only_event()
    assert ev.get("message") == "realtime_pipeline_error"
    assert ev.get("fingerprint") == ["realtime_pipeline_error", "SarvamCreditsExhausted"]
    assert ev["contexts"]["log_fields"] == {
        "provider": "sarvam",
        "error_type": "SarvamCreditsExhausted",
        "status_code": 402,
        "fatal": True,
        "call_id_hash": call_hash,
    }
    assert ev["tags"]["log_event"] == "realtime_pipeline_error"
    assert ev["logger"] == "app.realtime"
    assert "logentry" not in ev and "extra" not in ev
    assert name.split()[0] not in t.blob()
    assert "chest pain" not in t.blob()


def test_structlog_message_only_is_minimal(capture: Capture, slog: None) -> None:
    case_structlog_message_only(capture)


def test_structlog_message_only_test_fails_without_log_events(capture: Capture, slog: None) -> None:
    with pytest.raises(AssertionError):
        case_structlog_message_only(capture, disable=("log_events",))


def test_fstring_event_key_falls_back_to_code_location(capture: Capture, slog: None) -> None:
    name = fake_name()
    with capture() as t:
        structlog.get_logger("app.flow").error(f"lookup failed for {name}")
    ev = t.only_event()
    assert ev["message"] == "app.flow.test_fstring_event_key_falls_back_to_code_location"
    assert ev["fingerprint"][1] == "-"
    assert name.split()[0] not in t.blob() and "lookup failed" not in t.blob()


def test_stdlib_extra_allowlisted_fields(capture: Capture) -> None:
    name = fake_name()
    with capture() as t:
        logging.getLogger("qc.hms").error(
            "HMS poll auth error for %s",
            name,
            extra={"provider": "openemr", "http_status": 401, "complaint": name},
        )
    ev = t.only_event()
    assert ev["message"] == "qc.hms.test_stdlib_extra_allowlisted_fields"
    assert ev["contexts"]["log_fields"] == {"provider": "openemr", "http_status": 401}
    assert name.split()[0] not in t.blob() and "HMS poll" not in t.blob()


# V2e rate limit: a 900-line burst becomes a handful of events.
def case_rate_limit(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    log = logging.getLogger("app.burst")
    with capture(disable) as t:
        for _ in range(40):
            log.error("burst")
    assert len(t.events) == logcapture.PER_KEY_PER_MINUTE


def test_rate_limit_per_key(capture: Capture) -> None:
    case_rate_limit(capture)


def test_rate_limit_test_fails_without_limiter(capture: Capture) -> None:
    with pytest.raises(AssertionError):
        case_rate_limit(capture, disable=("log_rate_limit",))


def test_rate_limit_total_cap_and_window(
    capture: Capture, slog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(logcapture.rate_limiter, "clock", lambda: now[0])
    log = structlog.get_logger("app.many")
    with capture() as t:
        for i in range(50):
            for _ in range(3):
                log.error(f"distinct_failure_{i}")
        assert len(t.events) == logcapture.TOTAL_PER_MINUTE
        now[0] += 61
        log.error("after_window")
    assert len(t.events) == logcapture.TOTAL_PER_MINUTE + 1
    assert t.events[-1]["message"] == "after_window"


# structlog exc_info reaches the SDK as an exception event, exactly once.
def case_structlog_exc_info(capture: Capture, *, with_processor: bool = True) -> None:
    _configure_structlog(with_processor=with_processor)
    name, marker = fake_name(), f"GTSX{nonce()}"
    log = structlog.get_logger("app.db")
    try:
        with capture() as t:
            try:
                raise ValueError(marker)
            except ValueError as e:
                log.error("database_error", exc_info=e, complaint=name)
        ev = t.only_event()
        assert "exception" in ev, "no exception event"
        assert ev["exception"]["values"][-1]["type"] == "ValueError"
        assert marker in ev["exception"]["values"][-1]["value"]
        assert ev["tags"]["log_event"] == "database_error"
        assert ev["tags"]["logger"] == "app.db"
        assert name.split()[0] not in t.blob()
    finally:
        structlog.reset_defaults()


def test_structlog_exc_info_reaches_sdk(capture: Capture) -> None:
    case_structlog_exc_info(capture)


def test_structlog_exc_info_test_fails_without_processor(capture: Capture) -> None:
    with pytest.raises(AssertionError):
        case_structlog_exc_info(capture, with_processor=False)


@pytest.mark.parametrize("how", ["exception", "exc_info_true"])
def test_structlog_exception_is_sent_once(capture: Capture, slog: None, how: str) -> None:
    marker = f"GTSO{nonce()}"
    log = structlog.get_logger("app.op")
    with capture() as t:
        try:
            raise RuntimeError(marker)
        except RuntimeError:
            if how == "exception":
                log.exception("op_failed")
            else:
                log.error("op_failed", exc_info=True)
    ev = t.only_event()
    assert marker in ev["exception"]["values"][-1]["value"]


def test_processor_is_inert_without_exception_or_client() -> None:
    sentry_sdk.get_global_scope().set_client(None)
    d: dict[str, Any] = {"event": "x", "exc_info": True}
    assert structlog_capture_exc_info(None, "error", d) is d
    assert logcapture.CAPTURED_MARKER not in d
    d2: dict[str, Any] = {"event": "x", "exc_info": ValueError("y")}
    structlog_capture_exc_info(None, "info", d2)
    assert logcapture.CAPTURED_MARKER not in d2


def test_init_logs_service_env_never_dsn(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="sm_common.observability.init"), capture():
        pass
    assert "error tracking on: unit-prod" in caplog.text
    assert "example.ingest" not in caplog.text and "public@" not in caplog.text


def test_status_strings_kept_long_digit_runs_dropped() -> None:
    import types

    record = types.SimpleNamespace(
        msg={"event": "x", "status": "402", "http_status": "5xx", "status_code": "9876501234"}
    )
    assert logcapture.allowlisted_fields(record) == {"status": "402", "http_status": "5xx"}


def case_phone_shaped_numbers(capture: Capture, disable: tuple[str, ...] = ()) -> None:
    phone = "98" + "".join(str((int(c) * 7) % 10) for c in nonce_digits())
    with capture(disable) as t:
        logging.getLogger("app.num").error(
            "notify_failed",
            extra={
                "count": int(phone),
                "status_code": phone,
                "attempts": 3,
                "http_status": 502,
                "duration_ms": 1_800_000,
                "call_id_hash": phone,
                "tenant_id_hash": "3f9a0c1b2d4e",
            },
        )
    ev = t.only_event()
    assert ev["contexts"]["log_fields"] == {
        "attempts": 3,
        "http_status": 502,
        "duration_ms": 1_800_000,
        "tenant_id_hash": "3f9a0c1b2d4e",
    }
    assert phone not in t.blob()


def nonce_digits() -> str:
    import random

    return "".join(random.choices("0123456789", k=8))


def test_phone_shaped_numbers_never_ship(capture: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    # Prove the numeric caps (not redact_text's phone regex) are what drop them.
    from sm_common.observability import scrub

    monkeypatch.setattr(scrub, "redact_text", lambda s: s)
    case_phone_shaped_numbers(capture)


def test_phone_shaped_numbers_test_fails_without_caps(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sm_common.observability import scrub

    monkeypatch.setattr(scrub, "redact_text", lambda s: s)
    monkeypatch.setattr(logcapture, "NUMERIC_ABS_LIMIT", 10**30)
    monkeypatch.setattr(logcapture, "_LONG_DIGITS", __import__("re").compile(r"(?!x)x"))
    with pytest.raises(AssertionError):
        case_phone_shaped_numbers(capture)
