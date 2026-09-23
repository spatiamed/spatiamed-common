from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import BOOKING_IDENTIFIER_SYSTEM, FhirR4Adapter
from sm_common.integrations.canonical_types import AppointmentWrite
from sm_common.integrations.exceptions import (
    ConflictError,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)

BID = UUID("00000000-0000-0000-0000-0000000000b1")
START = datetime(2026, 9, 24, 4, 30, tzinfo=UTC)
TOKEN = f"{BOOKING_IDENTIFIER_SYSTEM}|{BID}"


def _adapter(handler):
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"}
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _write(practitioner="prac-1"):
    return AppointmentWrite(
        BID, "pat-1", practitioner, START, START + timedelta(minutes=15), "Fever"
    )


def _appt(id_="appt-9", start="2026-09-24T04:30:00+00:00"):
    return {"resourceType": "Appointment", "id": id_, "start": start}


def _bundle(resources):
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


@pytest.mark.asyncio
async def test_creates_a_real_fhir_appointment_with_our_identifier():
    seen = {}

    def handler(request):
        if request.method == "GET":
            assert request.url.params["identifier"] == TOKEN
            return httpx.Response(200, json=_bundle([]))
        seen["body"] = json.loads(request.content)
        seen["if_none_exist"] = request.headers.get("If-None-Exist")
        assert request.method == "POST" and request.url.path == "/fhir/Appointment"
        return httpx.Response(201, json=_appt())

    r = await _adapter(handler).write_back_idempotent(_write())
    body = seen["body"]
    assert body["resourceType"] == "Appointment" and body["status"] == "booked"
    assert body["start"] == START.isoformat()
    assert body["end"] == (START + timedelta(minutes=15)).isoformat()
    assert body["identifier"] == [{"system": BOOKING_IDENTIFIER_SYSTEM, "value": str(BID)}]
    actors = [p["actor"]["reference"] for p in body["participant"]]
    assert actors == ["Patient/pat-1", "Practitioner/prac-1"]
    assert seen["if_none_exist"] == f"identifier={TOKEN}"
    assert (r.status, r.hms_booking_id, r.created, r.hms_start) == (
        "SUCCESS",
        "appt-9",
        True,
        START,
    )


@pytest.mark.asyncio
async def test_existing_appointment_found_by_identifier_is_returned_without_posting():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("must not POST when our appointment already exists")
        return httpx.Response(200, json=_bundle([_appt("appt-old")]))

    r = await _adapter(handler).write_back_idempotent(_write())
    assert (r.hms_booking_id, r.created) == ("appt-old", False)


@pytest.mark.asyncio
async def test_two_existing_appointments_with_our_identifier_is_a_conflict():
    def handler(request):
        return httpx.Response(200, json=_bundle([_appt("a"), _appt("b")]))

    with pytest.raises(ConflictError):
        await _adapter(handler).write_back_idempotent(_write())


@pytest.mark.asyncio
async def test_unsupported_identifier_search_falls_back_to_conditional_create():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(400, json={"resourceType": "OperationOutcome"})
        return httpx.Response(201, json=_appt())

    r = await _adapter(handler).write_back_idempotent(_write())
    assert r.hms_booking_id == "appt-9"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [404, 405])
async def test_no_create_route_is_write_not_supported(code):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle([]))
        return httpx.Response(code, json={"message": "Route not found"})

    with pytest.raises(WriteNotSupported):
        await _adapter(handler).write_back_idempotent(_write())


@pytest.mark.asyncio
async def test_2xx_without_an_appointment_id_is_never_success():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle([]))
        return httpx.Response(200, json={"resourceType": "OperationOutcome"})

    with pytest.raises(VendorRejected):
        await _adapter(handler).write_back_idempotent(_write())


@pytest.mark.asyncio
async def test_5xx_is_transient():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle([]))
        return httpx.Response(503)

    with pytest.raises(TransientError):
        await _adapter(handler).write_back_idempotent(_write())


@pytest.mark.asyncio
async def test_no_practitioner_sends_only_the_patient_participant():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle([]))
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_appt())

    await _adapter(handler).write_back_idempotent(_write(practitioner=None))
    assert [p["actor"]["reference"] for p in seen["body"]["participant"]] == ["Patient/pat-1"]
