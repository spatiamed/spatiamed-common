from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from sm_common.integrations.adapters.openemr import OpenEmrAdapter
from sm_common.integrations.canonical_types import AppointmentWrite
from sm_common.integrations.exceptions import AuthError, ConflictError, VendorRejected

FIX = pathlib.Path(__file__).parents[1] / "fixtures" / "openemr"
BID = UUID("00000000-0000-0000-0000-0000000000f1")
START = datetime(2026, 9, 24, 4, 30, tzinfo=UTC)  # 10:00 in Asia/Kolkata
MARKER = f"[spatiamed:{BID}]"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


def _openemr_cfg():
    return {
        "timezone": "Asia/Kolkata",
        "pc_catid": "5",
        "pc_facility": "3",
        "pc_billing_location": "3",
        "write_user": {
            "token_url": "https://oe/t",
            "client_id": "c",
            "client_secret": "s",
            "username": "u",
            "password": "p",
        },
    }


def _adapter(handler):
    a = OpenEmrAdapter(
        base_url="https://oe/apis/default/fhir",
        auth_scheme="bearer",
        auth_cfg={"bearer_token": "sys"},
        openemr=_openemr_cfg(),
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _write(practitioner="prac-uuid"):
    return AppointmentWrite(
        BID, "pat-uuid", practitioner, START, START + timedelta(minutes=15), "Fever"
    )


class Router:
    """Serves recorded fixtures by route; records every request."""

    def __init__(
        self,
        listing_rows=None,
        get_rows=None,
        fhir_appointment=None,
        fhir_status=200,
    ):
        self.calls = []
        self.listing_rows = listing_rows
        self.get_rows = get_rows or {}
        self.fhir_appointment = fhir_appointment
        self.fhir_status = fhir_status

    def __call__(self, request):
        path, m = request.url.path, request.method
        self.calls.append((m, path, request.content))
        if path.endswith("/oauth2/default/token") or path == "/t":
            return httpx.Response(200, json={"access_token": "user-tok", "expires_in": 3600})
        if m == "GET" and path.startswith("/apis/default/fhir/Appointment/"):
            if self.fhir_status != 200:
                return httpx.Response(self.fhir_status)
            return httpx.Response(
                200, json=self.fhir_appointment or {"resourceType": "Appointment"}
            )
        if m == "GET" and path == "/apis/default/api/patient/pat-uuid":
            f = fixture("patient_get")
            return httpx.Response(f["status"], json=f["body"])
        if m == "GET" and path == "/apis/default/api/practitioner/prac-uuid":
            f = fixture("practitioner_get")
            return httpx.Response(f["status"], json=f["body"])
        if m == "GET" and path.endswith("/appointment") and "/patient/" in path:
            body = fixture("patient_appointments_list")["body"]
            if self.listing_rows is not None:
                body = self.listing_rows  # real shape is a bare list, not {"data": ...}
            return httpx.Response(200, json=body)
        if m == "POST" and path.endswith("/appointment"):
            f = fixture("appointment_post")
            return httpx.Response(f["status"], json=f["body"])
        if m == "DELETE" and "/patient/" in path and "/appointment/" in path:
            f = fixture("appointment_delete")
            return httpx.Response(f["status"], json=f["body"])
        if m == "GET" and "/apis/default/api/appointment/" in path:
            eid = path.rsplit("/", 1)[1]
            if eid in self.get_rows:
                return httpx.Response(200, json=[self.get_rows[eid]])
            f = fixture("appointment_get")
            return httpx.Response(f["status"], json=f["body"])
        raise AssertionError(f"unexpected {m} {path}")


def _pid():
    return str(fixture("patient_get")["body"]["data"]["pid"])


def _aid():
    return str(fixture("practitioner_get")["body"]["data"]["id"])


@pytest.mark.asyncio
async def test_start_is_sent_in_the_integration_timezone():
    r = Router(listing_rows=[])
    await _adapter(r).write_back_idempotent(_write())
    body = json.loads(
        next(c for c in r.calls if c[0] == "POST" and c[1].endswith("/appointment"))[2]
    )
    assert body["pc_eventDate"] == "2026-09-24"
    assert body["pc_startTime"] == "10:00"
    assert body["pc_duration"] == "900"
    assert body["pc_aid"] == _aid()
    assert MARKER in body["pc_hometext"] and body["pc_hometext"].startswith("Fever")
    assert (body["pc_catid"], body["pc_facility"], body["pc_billing_location"]) == ("5", "3", "3")


@pytest.mark.asyncio
async def test_create_returns_the_fhir_id_not_the_numeric_eid():
    """ingest dedupes on the FHIR Appointment.id (pc_uuid); returning pc_eid re-imports our own write."""
    r = Router(listing_rows=[])
    res = await _adapter(r).write_back_idempotent(_write())
    expected_uuid = fixture("appointment_get")["body"][0]["pc_uuid"]
    assert res.hms_booking_id == expected_uuid
    assert res.created is True
    assert res.hms_start is not None and res.hms_start.tzinfo is not None


@pytest.mark.asyncio
async def test_marker_found_returns_existing_without_posting():
    row = {
        "pc_eid": "77",
        "pc_eventDate": "2026-09-24",
        "pc_startTime": "10:00:00",
        "pc_aid": _aid(),
        "pc_uuid": "uuid-77",
    }
    r = Router(listing_rows=[row], get_rows={"77": {**row, "pc_hometext": f"Fever {MARKER}"}})
    res = await _adapter(r).write_back_idempotent(_write())
    assert (res.hms_booking_id, res.created) == ("uuid-77", False)
    assert not any(c[0] == "POST" and c[1].endswith("/appointment") for c in r.calls)


@pytest.mark.asyncio
async def test_same_slot_without_our_marker_is_a_conflict_not_a_double_booking():
    row = {
        "pc_eid": "78",
        "pc_eventDate": "2026-09-24",
        "pc_startTime": "10:00:00",
        "pc_aid": _aid(),
        "pc_uuid": "uuid-78",
    }
    r = Router(listing_rows=[row], get_rows={"78": {**row, "pc_hometext": "booked at the desk"}})
    with pytest.raises(ConflictError):
        await _adapter(r).write_back_idempotent(_write())


@pytest.mark.asyncio
async def test_marker_guard_ignores_other_dates_and_doctors():
    rows = [
        {
            "pc_eid": "1",
            "pc_eventDate": "2026-09-23",
            "pc_startTime": "10:00:00",
            "pc_aid": _aid(),
            "pc_uuid": "u1",
        },
        {
            "pc_eid": "2",
            "pc_eventDate": "2026-09-24",
            "pc_startTime": "10:00:00",
            "pc_aid": "999",
            "pc_uuid": "u2",
        },
        {
            "pc_eid": "3",
            "pc_eventDate": "2026-09-24",
            "pc_startTime": "11:00:00",
            "pc_aid": _aid(),
            "pc_uuid": "u3",
        },
    ]
    r = Router(listing_rows=rows)
    res = await _adapter(r).write_back_idempotent(_write())
    assert res.created is True
    single_gets = [c for c in r.calls if c[0] == "GET" and "/api/appointment/" in c[1]]
    # only the freshly created appointment is read back — none of the three unrelated rows
    assert len(single_gets) == 1


@pytest.mark.asyncio
async def test_validation_map_body_is_vendor_rejected():
    f = fixture("appointment_post_missing_hometext")

    class R(Router):
        def __call__(self, request):
            if request.method == "POST" and request.url.path.endswith("/appointment"):
                return httpx.Response(f["status"], json=f["body"])
            return super().__call__(request)

    with pytest.raises(VendorRejected):
        await _adapter(R(listing_rows=[])).write_back_idempotent(_write())


@pytest.mark.asyncio
async def test_a_401_on_the_standard_api_re_mints_the_token_once():
    state = {"n": 0}

    class R(Router):
        def __call__(self, request):
            if request.url.path == "/apis/default/api/patient/pat-uuid":
                state["n"] += 1
                if state["n"] == 1:
                    return httpx.Response(401)
            return super().__call__(request)

    r = R(listing_rows=[])
    await _adapter(r).write_back_idempotent(_write())
    assert sum(1 for c in r.calls if c[1] == "/t") == 2


@pytest.mark.asyncio
async def test_403_on_the_appointment_list_raises_auth_error_not_attributeerror():
    """A 403 body is a dict, not a row list — iterating it must not crash with AttributeError."""

    class R(Router):
        def __call__(self, request):
            path, m = request.url.path, request.method
            if m == "GET" and path.endswith("/appointment") and "/patient/" in path:
                return httpx.Response(403, json={"error": "forbidden"})
            return super().__call__(request)

    with pytest.raises(AuthError):
        await _adapter(R(listing_rows=[])).write_back_idempotent(_write())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_status", ["x", "%"])
async def test_cancelled_same_slot_row_does_not_block_rebooking(cancelled_status):
    row = {
        "pc_eid": "50",
        "pc_eventDate": "2026-09-24",
        "pc_startTime": "10:00:00",
        "pc_aid": _aid(),
        "pc_uuid": "uuid-50",
        "pc_apptstatus": cancelled_status,
    }
    r = Router(listing_rows=[row])
    res = await _adapter(r).write_back_idempotent(_write())
    assert res.created is True
    # the cancelled row is filtered out of same_slot entirely — never read back
    single_gets = [c for c in r.calls if c[0] == "GET" and "/api/appointment/" in c[1]]
    assert len(single_gets) == 1  # only the freshly created appointment


@pytest.mark.asyncio
async def test_cancel_deletes_the_matching_appointment_by_eid():
    row = {
        "pc_eid": "146",
        "pc_uuid": "a2d0fe82-5b70-4cb6-b6bd-52594079f973",
        "pc_eventDate": "2026-10-23",
        "pc_startTime": "10:00:00",
        "pc_aid": "1",
    }
    fhir_appt = {
        "resourceType": "Appointment",
        "participant": [{"actor": {"reference": "Patient/pat-uuid"}}],
    }
    r = Router(listing_rows=[row], fhir_appointment=fhir_appt)
    res = await _adapter(r).cancel(row["pc_uuid"], "patient requested")
    assert res.status == "SUCCESS"
    deletes = [c for c in r.calls if c[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][1].endswith(f"/appointment/{row['pc_eid']}")


@pytest.mark.asyncio
async def test_cancel_appointment_not_in_patients_list_is_not_found():
    fhir_appt = {
        "resourceType": "Appointment",
        "participant": [{"actor": {"reference": "Patient/pat-uuid"}}],
    }
    r = Router(listing_rows=[], fhir_appointment=fhir_appt)
    res = await _adapter(r).cancel("uuid-not-present", "patient requested")
    assert res.status == "NOT_FOUND"
    assert not any(c[0] == "DELETE" for c in r.calls)


@pytest.mark.asyncio
async def test_cancel_with_no_patient_participant_is_not_found_with_zero_standard_api_calls():
    fhir_appt = {"resourceType": "Appointment", "participant": []}
    r = Router(fhir_appointment=fhir_appt)
    res = await _adapter(r).cancel("some-uuid", "patient requested")
    assert res.status == "NOT_FOUND"
    assert not any("/apis/default/api/" in c[1] for c in r.calls)


@pytest.mark.asyncio
async def test_cancel_lookup_403_raises_auth_error_not_transient():
    r = Router(fhir_status=403)
    with pytest.raises(AuthError):
        await _adapter(r).cancel("some-uuid", "patient requested")
