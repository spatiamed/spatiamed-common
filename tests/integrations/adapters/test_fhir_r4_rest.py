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

from sm_common.integrations.exceptions import AuthError, TransientError, VendorRejected
from sm_common.phone import hash_phone_for_lookup
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
async def test_fetch_doctor_roster_http_error_raises_transient():
    """A failed fetch must never read as "this hospital has no doctors": the
    roster snapshot keys mapping staleness off it (QueueCare sub-project 2)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    a = _adapter(handler)
    with pytest.raises(TransientError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))


def _oauth_adapter(handler):
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="oauth2_client_credentials",
        auth_cfg={"token_url": "https://hms.example/token", "client_id": "c", "client_secret": "s"},
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


@pytest.mark.asyncio
async def test_fetch_doctor_roster_token_refusal_raises_auth_error():
    """Rotated or wrong credentials: the token endpoint refuses. That must reach
    QueueCare as AuthError (an error run the admin can read), not a raw httpx
    error that the refresh endpoint turns into a bare 500 (final review I-1)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(401, json={"error": "invalid_client"})
        raise AssertionError("roster must not be fetched without a token")

    a = _oauth_adapter(handler)
    with pytest.raises(AuthError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))


@pytest.mark.asyncio
async def test_fetch_doctor_roster_token_endpoint_unreachable_raises_transient():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    a = _oauth_adapter(handler)
    with pytest.raises(TransientError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))


@pytest.mark.asyncio
async def test_fetch_doctor_roster_token_endpoint_other_4xx_raises_transient():
    """A status the auth layer does not classify (404: wrong token_url) still
    surfaces as a typed adapter error, never a raw httpx one."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such route")

    a = _oauth_adapter(handler)
    with pytest.raises(TransientError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))


@pytest.mark.asyncio
async def test_fetch_doctor_roster_token_non_json_raises_transient():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    a = _oauth_adapter(handler)
    with pytest.raises(TransientError):
        await a.fetch_doctor_roster(as_of_date=date(2026, 6, 22))


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
async def test_find_patient_hashes_the_telecom_phone():
    """CanonicalPatient.phone_hash must hold a salted hash, never the raw number.

    A live OpenEMR returned telecom [{"system": "phone", "value": "9000000001"}]
    and the adapter passed that straight into phone_hash. Two consequences: the
    value can never match a stored hash, and an unhashed number travels into logs
    and storage under a name asserting it is hashed.
    """
    raw = "9000000001"
    salt = "test-salt"

    def handler(req: httpx.Request) -> httpx.Response:
        patient = _patient_resource(resource_id="pat-9", mrn="pat-9")
        patient["telecom"] = [{"system": "phone", "value": raw, "use": "mobile"}]
        return httpx.Response(200, json=_patient_bundle([{"resource": patient}]))

    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="bearer",
        auth_cfg={"bearer_token": "t"},
        hash_salt=salt,
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await a.find_patient(mrn="pat-9")
    assert result is not None
    assert result.phone_hash != raw, "raw phone number leaked into phone_hash"
    assert result.phone_hash == hash_phone_for_lookup(raw, salt)


# ─── outbound failures must escalate, not be absorbed ─────────────────────────


@pytest.mark.asyncio
async def test_cancel_server_error_raises_so_compensation_retries():
    """A failed compensation must retry, not report failure and move on.

    Swallowing it leaves a live booking in QueueCare and nothing in the HMS —
    the exact split-brain the saga exists to prevent.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    with pytest.raises(TransientError):
        await _adapter(handler).cancel(hms_booking_id="appt-x", reason="reason")


@pytest.mark.asyncio
async def test_cancel_not_found_is_still_a_returned_outcome():
    """Nothing to cancel is an answer, not an error — do not retry it."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    result = await _adapter(handler).cancel(hms_booking_id="gone", reason="r")
    assert result.status == "NOT_FOUND"


@pytest.mark.asyncio
async def test_push_visit_event_non_2xx_raises():
    """A visit event that never landed must not look like one that did.

    Against OpenEMR this is a 404 on every call, previously logged at warning
    and discarded, so the caller had no way to know the HMS never saw it.
    """
    from sm_common.integrations.canonical_types import VisitConsultationStarted

    event = VisitConsultationStarted(
        event_uuid=uuid4(),
        appointment_id="appt-1",
        started_at=datetime.now(UTC),
        actual_doctor_external_id="doc-1",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Error")

    with pytest.raises(TransientError):
        await _adapter(handler).push_visit_event(event)


@pytest.mark.asyncio
async def test_cancel_permanent_4xx_raises_not_returned():
    """Spec §1: cancel raises on failure; only NOT_FOUND is a returned outcome.
    A permanent 4xx is VendorRejected (terminal), a 5xx TransientError."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="cannot cancel a finished appointment")

    with pytest.raises(VendorRejected, match="422"):
        await _adapter(handler).cancel(hms_booking_id="appt-x", reason="r")


# ─── get_patient tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_patient_reads_by_resource_id():
    """Resolution needs a direct read, not an identifier search.

    Measured against OpenEMR 8.3.0: GET /Patient?identifier=<resource-uuid>
    returns 0 entries because its identifier is the internal pid, while
    GET /Patient/<resource-uuid> returns 200 with the telecom.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/Patient/pat-42"), f"expected a direct read, got {req.url}"
        assert not req.url.params, f"a direct read takes no search params, got {req.url.params}"
        return httpx.Response(200, json=_patient_resource(resource_id="pat-42", mrn="MRN-9"))

    result = await _adapter(handler).get_patient("pat-42")
    assert result is not None
    assert result.mrn == "MRN-9"


@pytest.mark.asyncio
async def test_get_patient_returns_none_when_absent():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    assert await _adapter(handler).get_patient("nope") is None


@pytest.mark.asyncio
async def test_get_patient_default_is_none_for_adapters_without_direct_read():
    """bahmni/mocdoc/generic_rest/csv_import inherit the default rather than break."""
    from sm_common.integrations.adapters.generic_rest import GenericRestAdapter

    adapter = GenericRestAdapter({"base_url": "https://x", "list_appointments_path": "/a"})
    assert await adapter.get_patient("anything") is None
