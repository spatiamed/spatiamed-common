import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.canonical_types import AppointmentWrite
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
SEARCH = f"{REST}/appointments/search"
POST = f"{REST}/appointment"
IST = timezone(timedelta(hours=5, minutes=30))
BOOKING = UUID("aaaaaaaa-0000-4000-8000-000000000001")
PATIENT = "11111111-aaaa-4aaa-8aaa-000000000001"
PRACT = "22222222-bbbb-4bbb-8bbb-000000000002"
SERVICE = "33333333-cccc-4ccc-8ccc-000000000003"
EXISTING = "44444444-dddd-4ddd-8ddd-000000000004"
START = datetime(2026, 10, 25, 10, 30, tzinfo=IST)
START_MS = 1792904400000  # 2026-10-25T05:00:00Z
END_MS = START_MS + 15 * 60_000


def _adapter(**cfg: object) -> BahmniAdapter:
    cfg.setdefault("default_service_uuid", SERVICE)
    config = BahmniConfig(
        location_uuid="LOC",
        identifier_type_uuid="T",
        identifier_source_uuid="S",
        **cfg,  # type: ignore[arg-type]
    )
    return BahmniAdapter(
        http=BahmniHttp(BASE, "u", "p"), config=config, hash_salt="s", transport_key="k"
    )


def _write(**over: object) -> AppointmentWrite:
    base: dict[str, object] = dict(
        booking_id=BOOKING,
        patient_ref=PATIENT,
        practitioner_ref=PRACT,
        start=START,
        end=START + timedelta(minutes=15),
        reason="fever",
    )
    base.update(over)
    return AppointmentWrite(**base)  # type: ignore[arg-type]


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _row(status: str, comments: str = f"sm:{BOOKING} fever", uuid: str = EXISTING) -> dict:  # type: ignore[type-arg]
    return {
        "uuid": uuid,
        "status": status,
        "comments": comments,
        "startDateTime": START_MS,
        "endDateTime": END_MS,
    }


def _created(start_ms: int = START_MS) -> httpx.Response:
    return httpx.Response(
        200,
        json={"uuid": "new-uuid", "startDateTime": start_ms, "endDateTime": end_ms(start_ms)},
    )


def end_ms(s: int) -> int:
    return s + 15 * 60_000


@respx.mock
async def test_write_payload_matches_recipe():
    _login()
    search = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(POST).mock(return_value=_created())
    await _adapter().write_back_idempotent(_write())
    assert json.loads(search.calls[0].request.content) == {
        "startDate": "2026-10-25T04:59:00+00:00",
        "endDate": "2026-10-25T05:16:00+00:00",
    }
    assert json.loads(post.calls[0].request.content) == {
        "patientUuid": PATIENT,
        "serviceUuid": SERVICE,
        "providers": [{"uuid": PRACT, "response": "ACCEPTED"}],
        "locationUuid": "LOC",
        "startDateTime": START_MS,
        "endDateTime": END_MS,
        "appointmentKind": "Scheduled",
        "comments": f"sm:{BOOKING} fever",
    }


@respx.mock
async def test_no_practitioner_sends_empty_providers_and_bare_marker():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(POST).mock(return_value=_created())
    await _adapter().write_back_idempotent(_write(practitioner_ref=None, reason=None))
    body = json.loads(post.calls[0].request.content)
    assert body["providers"] == []
    assert body["comments"] == f"sm:{BOOKING}"


@respx.mock
async def test_retry_while_active_returns_existing():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[_row("Scheduled")]))
    post = respx.post(POST).mock(return_value=_created())
    r = await _adapter().write_back_idempotent(_write())
    assert (r.status, r.hms_booking_id, r.created) == ("SUCCESS", EXISTING, False)
    assert r.hms_start == START.astimezone(timezone.utc)
    assert not post.called


@respx.mock
async def test_retry_after_cancel_creates_new():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[_row("Cancelled")]))
    post = respx.post(POST).mock(return_value=_created())
    r = await _adapter().write_back_idempotent(_write())
    assert post.called
    assert (r.hms_booking_id, r.created) == ("new-uuid", True)


@respx.mock
async def test_missed_is_terminal_creates_new():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[_row("Missed")]))
    post = respx.post(POST).mock(return_value=_created())
    r = await _adapter().write_back_idempotent(_write())
    assert post.called and r.created is True


@respx.mock
async def test_other_booking_marker_is_ignored():
    _login()
    other = _row("Scheduled", comments="sm:bbbbbbbb-0000-4000-8000-000000000009")
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[other]))
    post = respx.post(POST).mock(return_value=_created())
    await _adapter().write_back_idempotent(_write())
    assert post.called


@respx.mock
async def test_hms_start_from_epoch_echo():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    # Bahmni may echo a different instant than requested; we report what it recorded.
    respx.post(POST).mock(return_value=_created(START_MS + 3_600_000))
    r = await _adapter().write_back_idempotent(_write())
    assert r.hms_start == datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)


@respx.mock
async def test_400_is_vendor_rejected_not_transient():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    respx.post(POST).mock(
        return_value=httpx.Response(400, json={"error": {"message": "slot already booked"}})
    )
    with pytest.raises(VendorRejected) as ei:
        await _adapter().write_back_idempotent(_write())
    assert not isinstance(ei.value, ConflictError)


@respx.mock
async def test_unmapped_practitioner_without_default_service_raises_config_error():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(POST).mock(return_value=_created())
    with pytest.raises(ConfigError):
        await _adapter(default_service_uuid="").write_back_idempotent(_write())
    assert not post.called


@respx.mock
async def test_service_ref_overrides_config_service():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(POST).mock(return_value=_created())
    await _adapter().write_back_idempotent(_write(service_ref="svc-explicit"))
    assert json.loads(post.calls[0].request.content)["serviceUuid"] == "svc-explicit"


@respx.mock
async def test_practitioner_mapped_service_used():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(POST).mock(return_value=_created())
    await _adapter(service_by_practitioner={PRACT: "svc-mapped"}).write_back_idempotent(_write())
    assert json.loads(post.calls[0].request.content)["serviceUuid"] == "svc-mapped"


@respx.mock
async def test_search_at_row_cap_fails_closed_without_post():
    _login()
    rows = [_row("Scheduled", comments=f"other-{i}", uuid=f"u{i}") for i in range(50)]
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=rows))
    post = respx.post(POST).mock(return_value=_created())
    with pytest.raises(TransientError):
        await _adapter().write_back_idempotent(_write())
    assert not post.called


@respx.mock
async def test_search_404_fails_closed_without_post():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(404))
    post = respx.post(POST).mock(return_value=_created())
    with pytest.raises(WriteNotSupported):
        await _adapter().write_back_idempotent(_write())
    assert not post.called


@respx.mock
async def test_search_non_list_body_fails_closed_without_post():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json={"error": "x"}))
    post = respx.post(POST).mock(return_value=_created())
    with pytest.raises(TransientError):
        await _adapter().write_back_idempotent(_write())
    assert not post.called


@respx.mock
async def test_marker_prefix_of_longer_token_is_not_a_match():
    _login()
    longer = _row("Scheduled", comments=f"sm:{BOOKING}extra")
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[longer]))
    post = respx.post(POST).mock(return_value=_created())
    await _adapter().write_back_idempotent(_write())
    assert post.called


@respx.mock
async def test_bare_marker_matches():
    _login()
    respx.post(SEARCH).mock(
        return_value=httpx.Response(200, json=[_row("Scheduled", comments=f"sm:{BOOKING}")])
    )
    post = respx.post(POST).mock(return_value=_created())
    r = await _adapter().write_back_idempotent(_write())
    assert r.created is False and not post.called


@respx.mock
async def test_write_5xx_raises_transient():
    _login()
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=[]))
    respx.post(POST).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(TransientError):
        await _adapter().write_back_idempotent(_write())


@respx.mock
async def test_two_live_marker_rows_conflict_landed():
    _login()
    rows = [_row("Scheduled"), _row("CheckedIn", uuid="55555555-eeee-4eee-8eee-000000000005")]
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=rows))
    post = respx.post(POST).mock(return_value=_created())
    with pytest.raises(ConflictError) as ei:
        await _adapter().write_back_idempotent(_write())
    assert ei.value.landed is True
    assert not post.called
