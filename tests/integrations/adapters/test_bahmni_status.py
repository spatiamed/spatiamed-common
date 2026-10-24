import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.canonical_types import (
    VisitCheckedIn,
    VisitConsultationStarted,
    VisitFinalized,
)
from sm_common.integrations.exceptions import NotFound, TransientError

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
APT = "44444444-dddd-4ddd-8ddd-000000000004"
GET = f"{REST}/appointments/{APT}"
CHANGE = f"{REST}/appointments/{APT}/status-change"
NOW = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)


def _adapter() -> BahmniAdapter:
    config = BahmniConfig(
        location_uuid="LOC", identifier_type_uuid="T", identifier_source_uuid="S"
    )
    return BahmniAdapter(
        http=BahmniHttp(BASE, "u", "p"), config=config, hash_salt="s", transport_key="k"
    )


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _current(status: str) -> None:
    respx.get(GET).mock(return_value=httpx.Response(200, json={"uuid": APT, "status": status}))


@respx.mock
async def test_cancel_posts_status_change_with_timezone():
    _login()
    _current("Scheduled")
    post = respx.post(CHANGE).mock(return_value=httpx.Response(200, json={}))
    res = await _adapter().cancel(APT, "patient asked")
    assert res.status == "SUCCESS"
    assert json.loads(post.calls[0].request.content) == {
        "toStatus": "Cancelled",
        "onDate": None,
        "timeZone": "Asia/Kolkata",
        "comments": "patient asked",
    }


@respx.mock
async def test_cancel_already_cancelled_is_success_without_post():
    _login()
    _current("Cancelled")
    post = respx.post(CHANGE).mock(return_value=httpx.Response(500))
    res = await _adapter().cancel(APT, "x")
    assert res.status == "SUCCESS"
    assert post.call_count == 0


@respx.mock
async def test_cancel_404_not_found():
    _login()
    respx.get(GET).mock(return_value=httpx.Response(404))
    post = respx.post(CHANGE).mock(return_value=httpx.Response(200, json={}))
    res = await _adapter().cancel(APT, "x")
    assert res.status == "NOT_FOUND"
    assert post.call_count == 0


@respx.mock
async def test_cancel_transient_is_failed():
    _login()
    respx.get(GET).mock(return_value=httpx.Response(503))
    res = await _adapter().cancel(APT, "x")
    assert res.status == "FAILED"


@respx.mock
async def test_push_checked_in():
    _login()
    _current("Scheduled")
    post = respx.post(CHANGE).mock(return_value=httpx.Response(200, json={}))
    ev = VisitCheckedIn(
        event_uuid=uuid4(), appointment_id=APT, queuecare_visit_id=uuid4(),
        arrived_at=NOW, token_number="A1",
    )
    await _adapter().push_visit_event(ev)
    assert json.loads(post.calls[0].request.content) == {
        "toStatus": "CheckedIn",
        "onDate": None,
        "timeZone": "Asia/Kolkata",
    }


@respx.mock
async def test_push_consultation_started_noop():
    _login()
    get = respx.get(GET).mock(return_value=httpx.Response(200, json={"status": "CheckedIn"}))
    post = respx.post(CHANGE).mock(return_value=httpx.Response(200, json={}))
    ev = VisitConsultationStarted(
        event_uuid=uuid4(), appointment_id=APT, started_at=NOW, actual_doctor_external_id="d"
    )
    await _adapter().push_visit_event(ev)
    assert post.call_count == 0
    assert get.call_count == 0


@pytest.mark.parametrize(
    ("final", "to"),
    [("completed", "Completed"), ("no_show", "Missed"), ("cancelled_after_arrival", "Cancelled")],
)
@respx.mock
async def test_push_finalized_maps_status(final, to):
    _login()
    _current("CheckedIn")
    post = respx.post(CHANGE).mock(return_value=httpx.Response(200, json={}))
    ev = VisitFinalized(event_uuid=uuid4(), appointment_id=APT, final_status=final, finalized_at=NOW)
    await _adapter().push_visit_event(ev)
    assert json.loads(post.calls[0].request.content)["toStatus"] == to


@respx.mock
async def test_push_already_at_status_skips_post():
    _login()
    _current("Missed")
    post = respx.post(CHANGE).mock(return_value=httpx.Response(500))
    ev = VisitFinalized(
        event_uuid=uuid4(), appointment_id=APT, final_status="no_show", finalized_at=NOW
    )
    await _adapter().push_visit_event(ev)
    assert post.call_count == 0


@respx.mock
async def test_push_unknown_appointment_raises_not_found():
    _login()
    respx.get(GET).mock(return_value=httpx.Response(404))
    ev = VisitFinalized(
        event_uuid=uuid4(), appointment_id=APT, final_status="completed", finalized_at=NOW
    )
    with pytest.raises(NotFound):
        await _adapter().push_visit_event(ev)


@respx.mock
async def test_push_transient_propagates():
    _login()
    respx.get(GET).mock(return_value=httpx.Response(503))
    ev = VisitFinalized(
        event_uuid=uuid4(), appointment_id=APT, final_status="completed", finalized_at=NOW
    )
    with pytest.raises(TransientError):
        await _adapter().push_visit_event(ev)
