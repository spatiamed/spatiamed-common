"""Tests for FhirR4Adapter Task-3 methods:
find_patient, fetch_doctor_roster, fetch_recent_bookings,
write_back_idempotent, cancel, push_visit_event.

All tests use httpx.MockTransport — no external calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter


def _adapter(handler):
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="bearer",
        auth_cfg={"bearer_token": "t"},
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _patient_bundle(entries: list) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": entries,
    }


def _patient_resource(
    resource_id: str = "pat-1",
    mrn: str = "pat-1",
    abha_id: str | None = None,
    name_text: str = "Test Patient",
    gender: str = "male",
    birth_date: str | None = "1990-06-15",
    phone: str | None = "+919876543210",
) -> dict:
    identifiers = [{"system": "urn:local:mrn", "value": mrn}]
    if abha_id:
        identifiers.append({"system": "https://ndhm.gov.in", "value": abha_id})
    resource: dict = {
        "resourceType": "Patient",
        "id": resource_id,
        "identifier": identifiers,
        "name": [{"text": name_text}],
        "gender": gender,
    }
    if birth_date:
        resource["birthDate"] = birth_date
    if phone:
        resource["telecom"] = [{"system": "phone", "value": phone}]
    return resource


def _practitioner_bundle(entries: list) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": entries,
    }


def _practitioner_resource(
    resource_id: str = "doc-1",
    name_text: str = "Dr. Test",
    speciality_code: str = "394814009",
    speciality_display: str = "General Practice",
    dept_code: str = "dept-1",
) -> dict:
    return {
        "resourceType": "Practitioner",
        "id": resource_id,
        "name": [{"text": name_text}],
        "qualification": [
            {
                "code": {
                    "coding": [
                        {
                            "system": "http://snomed.info/sct",
                            "code": speciality_code,
                            "display": speciality_display,
                        }
                    ]
                },
                "identifier": [{"system": "urn:local:dept", "value": dept_code}],
            }
        ],
    }


def _appointment_bundle(entries: list) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": entries,
    }


def _appointment_resource(
    resource_id: str = "appt-1",
    status: str = "booked",
    doctor_id: str = "doc-1",
    updated_at: str = "2026-06-22T10:00:00+00:00",
    slot_start: str = "2026-06-23T09:30:00+05:30",
) -> dict:
    return {
        "resourceType": "Appointment",
        "id": resource_id,
        "status": status,
        "meta": {"lastUpdated": updated_at},
        "start": slot_start,
        "participant": [
            {"actor": {"reference": f"Practitioner/{doctor_id}"}},
        ],
    }


# ─── find_patient tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_patient_by_mrn():
    """find_patient(mrn=...) → returns CanonicalPatient with mrn field set."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Patient" in req.url.path
        assert "pat-1" in req.url.params.get("identifier", "")
        patient = _patient_resource(resource_id="pat-1", mrn="pat-1")
        return httpx.Response(200, json=_patient_bundle([{"resource": patient}]))

    a = _adapter(handler)
    result = await a.find_patient(mrn="pat-1")
    assert result is not None
    assert result.mrn == "pat-1"


@pytest.mark.asyncio
async def test_find_patient_returns_none_when_empty():
    """find_patient with no matching entries → returns None."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_patient_bundle([]))

    a = _adapter(handler)
    result = await a.find_patient(mrn="unknown-mrn")
    assert result is None


@pytest.mark.asyncio
async def test_find_patient_by_abha_id():
    """find_patient(abha_id=...) → returns CanonicalPatient with abha_id set."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "abha-123" in req.url.params.get("identifier", "")
        patient = _patient_resource(resource_id="pat-2", mrn="pat-2", abha_id="abha-123")
        return httpx.Response(200, json=_patient_bundle([{"resource": patient}]))

    a = _adapter(handler)
    result = await a.find_patient(abha_id="abha-123")
    assert result is not None
    assert result.abha_id == "abha-123"


@pytest.mark.asyncio
async def test_find_patient_maps_gender_male():
    """FHIR 'male' gender → 'M'."""

    def handler(req: httpx.Request) -> httpx.Response:
        patient = _patient_resource(gender="male")
        return httpx.Response(200, json=_patient_bundle([{"resource": patient}]))

    a = _adapter(handler)
    result = await a.find_patient(mrn="pat-1")
    assert result is not None
    assert result.gender == "M"


@pytest.mark.asyncio
async def test_find_patient_maps_gender_female():
    """FHIR 'female' gender → 'F'."""

    def handler(req: httpx.Request) -> httpx.Response:
        patient = _patient_resource(gender="female")
        return httpx.Response(200, json=_patient_bundle([{"resource": patient}]))

    a = _adapter(handler)
    result = await a.find_patient(mrn="pat-1")
    assert result is not None
    assert result.gender == "F"


@pytest.mark.asyncio
async def test_find_patient_http_error_returns_none():
    """find_patient HTTP error → returns None (never raises)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    a = _adapter(handler)
    result = await a.find_patient(mrn="pat-1")
    assert result is None


# ─── fetch_doctor_roster tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_doctor_roster_maps_practitioners():
    """fetch_doctor_roster returns list of CanonicalDoctor with expected external_id."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Practitioner" in req.url.path
        pract = _practitioner_resource(resource_id="doc-1")
        return httpx.Response(200, json=_practitioner_bundle([{"resource": pract}]))

    a = _adapter(handler)
    roster = await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))
    assert len(roster) == 1
    assert roster[0].external_doctor_id == "doc-1"


@pytest.mark.asyncio
async def test_fetch_doctor_roster_empty_bundle():
    """fetch_doctor_roster with empty bundle → empty list."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_practitioner_bundle([]))

    a = _adapter(handler)
    roster = await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))
    assert roster == []


@pytest.mark.asyncio
async def test_fetch_doctor_roster_http_error_returns_empty():
    """fetch_doctor_roster HTTP error → returns [] (never raises)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    a = _adapter(handler)
    roster = await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))
    assert roster == []


@pytest.mark.asyncio
async def test_fetch_doctor_roster_follows_next_link():
    """fetch_doctor_roster follows FHIR pagination next links."""
    page2_url = "https://hms.example/fhir/Practitioner?page=2"

    page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "link": [{"relation": "next", "url": page2_url}],
        "entry": [{"resource": _practitioner_resource(resource_id="doc-1")}],
    }
    page2 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": _practitioner_resource(resource_id="doc-2")}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    a = _adapter(handler)
    roster = await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))
    ids = {d.external_doctor_id for d in roster}
    assert "doc-1" in ids
    assert "doc-2" in ids
    assert len(roster) == 2


# ─── fetch_recent_bookings tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_recent_bookings_returns_list():
    """fetch_recent_bookings returns ExternalBooking list from FHIR Appointment."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Appointment" in req.url.path
        lu_param = req.url.params.get("_lastUpdated", "")
        assert lu_param.startswith("gt"), f"Expected gt... param, got: {lu_param}"
        appt = _appointment_resource(resource_id="appt-99")
        return httpx.Response(200, json=_appointment_bundle([{"resource": appt}]))

    hospital_id = uuid4()
    a = _adapter(handler)
    bookings = await a.fetch_recent_bookings(hospital_id=hospital_id, lookback_minutes=30)
    assert len(bookings) == 1
    assert bookings[0].appointment_id == "appt-99"


@pytest.mark.asyncio
async def test_fetch_recent_bookings_http_error_returns_empty():
    """fetch_recent_bookings HTTP error → returns [] (never raises)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    a = _adapter(handler)
    result = await a.fetch_recent_bookings(hospital_id=uuid4(), lookback_minutes=30)
    assert result == []


@pytest.mark.asyncio
async def test_fetch_recent_bookings_follows_next_link():
    """fetch_recent_bookings follows FHIR pagination next links."""
    page2_url = "https://hms.example/fhir/Appointment?page=2"

    page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "link": [{"relation": "next", "url": page2_url}],
        "entry": [{"resource": _appointment_resource(resource_id="appt-1")}],
    }
    page2 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": _appointment_resource(resource_id="appt-2")}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    a = _adapter(handler)
    bookings = await a.fetch_recent_bookings(hospital_id=uuid4(), lookback_minutes=30)
    ids = {b.appointment_id for b in bookings}
    assert "appt-1" in ids
    assert "appt-2" in ids
    assert len(bookings) == 2


# ─── write_back_idempotent tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_back_success():
    """200 response → WriteBackResult(status='SUCCESS')."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Appointment/" in req.url.path
        assert req.method == "PUT"
        return httpx.Response(200, json={"resourceType": "Appointment", "id": "appt-1"})

    a = _adapter(handler)
    booking_id = uuid4()
    result = await a.write_back_idempotent(
        booking_id=booking_id,
        payload={"appointment_id": "appt-1", "resourceType": "Appointment"},
        idempotency_key="idem-key-1",
    )
    assert result.status == "SUCCESS"


@pytest.mark.asyncio
async def test_write_back_created_maps_to_success():
    """201 response → WriteBackResult(status='SUCCESS')."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"resourceType": "Appointment", "id": "appt-new"})

    a = _adapter(handler)
    result = await a.write_back_idempotent(
        booking_id=uuid4(),
        payload={"appointment_id": "appt-new", "resourceType": "Appointment"},
        idempotency_key="idem-key-2",
    )
    assert result.status == "SUCCESS"
    assert result.hms_booking_id == "appt-new"


@pytest.mark.asyncio
async def test_write_back_conflict_maps_to_conflict_status():
    """409 response → WriteBackResult(status='CONFLICT')."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"issue": [{"diagnostics": "conflict"}]})

    a = _adapter(handler)
    result = await a.write_back_idempotent(
        booking_id=uuid4(),
        payload={"appointment_id": "appt-conflict"},
        idempotency_key="idem-key-3",
    )
    assert result.status == "CONFLICT"


@pytest.mark.asyncio
async def test_write_back_server_error_maps_to_transient_error():
    """5xx response → WriteBackResult(status='TRANSIENT_ERROR')."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    a = _adapter(handler)
    result = await a.write_back_idempotent(
        booking_id=uuid4(),
        payload={"appointment_id": "appt-x"},
        idempotency_key="idem-key-4",
    )
    assert result.status == "TRANSIENT_ERROR"


@pytest.mark.asyncio
async def test_write_back_idempotency_key_sent_as_header():
    """write_back_idempotent sends X-Idempotency-Key header."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("X-Idempotency-Key") == "idem-key-5"
        return httpx.Response(200, json={"resourceType": "Appointment", "id": "appt-1"})

    a = _adapter(handler)
    await a.write_back_idempotent(
        booking_id=uuid4(),
        payload={"appointment_id": "appt-1"},
        idempotency_key="idem-key-5",
    )


@pytest.mark.asyncio
async def test_write_back_uses_booking_id_when_no_appointment_id_in_payload():
    """write_back_idempotent falls back to booking_id when payload has no appointment_id."""
    booking_id = UUID("12345678-1234-5678-1234-567812345678")

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(booking_id) in req.url.path
        return httpx.Response(200, json={"resourceType": "Appointment", "id": str(booking_id)})

    a = _adapter(handler)
    result = await a.write_back_idempotent(
        booking_id=booking_id,
        payload={"resourceType": "Appointment"},
        idempotency_key="idem-key-6",
    )
    assert result.status == "SUCCESS"


# ─── cancel tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_success():
    """200 response → CancelResult(status='SUCCESS')."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Appointment/appt-1" in req.url.path
        assert req.method in ("PUT", "PATCH")
        return httpx.Response(200, json={"resourceType": "Appointment", "id": "appt-1"})

    a = _adapter(handler)
    result = await a.cancel(hms_booking_id="appt-1", reason="Patient requested")
    assert result.status == "SUCCESS"


@pytest.mark.asyncio
async def test_cancel_not_found():
    """404 response → CancelResult(status='NOT_FOUND')."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"issue": [{"diagnostics": "not found"}]})

    a = _adapter(handler)
    result = await a.cancel(hms_booking_id="appt-missing", reason="No show")
    assert result.status == "NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_server_error_maps_to_failed_status():
    """5xx response → CancelResult(status='FAILED')."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    a = _adapter(handler)
    result = await a.cancel(hms_booking_id="appt-x", reason="reason")
    assert result.status == "FAILED"


@pytest.mark.asyncio
async def test_cancel_sends_cancelled_status_in_body():
    """cancel PUT/PATCH body contains status='cancelled'."""
    import json as json_lib

    def handler(req: httpx.Request) -> httpx.Response:
        body = json_lib.loads(req.content)
        assert body.get("status") == "cancelled"
        return httpx.Response(200, json={"resourceType": "Appointment", "id": "appt-1"})

    a = _adapter(handler)
    await a.cancel(hms_booking_id="appt-1", reason="Test reason")


# ─── push_visit_event tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_visit_event_checked_in_posts_encounter():
    """push_visit_event for VisitCheckedIn → POST /Encounter with event_uuid."""
    from sm_common.integrations.canonical_types import VisitCheckedIn

    event_uuid = uuid4()
    event = VisitCheckedIn(
        event_uuid=event_uuid,
        appointment_id="appt-1",
        queuecare_visit_id=uuid4(),
        arrived_at=datetime.now(UTC),
        token_number="T-001",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Encounter" in req.url.path
        assert req.method == "POST"
        assert req.headers.get("X-Idempotency-Key") == str(event_uuid)
        return httpx.Response(201, json={"resourceType": "Encounter", "id": "enc-1"})

    a = _adapter(handler)
    result = await a.push_visit_event(event)
    assert result is None


@pytest.mark.asyncio
async def test_push_visit_event_finalized_posts_encounter():
    """push_visit_event for VisitFinalized → POST /Encounter."""
    from sm_common.integrations.canonical_types import VisitFinalized

    event_uuid = uuid4()
    event = VisitFinalized(
        event_uuid=event_uuid,
        appointment_id="appt-1",
        final_status="completed",
        finalized_at=datetime.now(UTC),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/Encounter" in req.url.path
        assert req.headers.get("X-Idempotency-Key") == str(event_uuid)
        return httpx.Response(201, json={"resourceType": "Encounter", "id": "enc-2"})

    a = _adapter(handler)
    result = await a.push_visit_event(event)
    assert result is None


@pytest.mark.asyncio
async def test_push_visit_event_non_2xx_swallowed():
    """push_visit_event swallows non-2xx responses (fire-and-forget)."""
    from sm_common.integrations.canonical_types import VisitConsultationStarted

    event = VisitConsultationStarted(
        event_uuid=uuid4(),
        appointment_id="appt-1",
        started_at=datetime.now(UTC),
        actual_doctor_external_id="doc-1",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Error")

    a = _adapter(handler)
    # Must not raise
    result = await a.push_visit_event(event)
    assert result is None
