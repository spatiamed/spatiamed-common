from datetime import date

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter

BUNDLE = {
    "resourceType": "Bundle",
    "type": "searchset",
    "entry": [
        {
            "resource": {
                "resourceType": "Appointment",
                "id": "appt-1",
                "status": "booked",
                "meta": {"versionId": "3", "lastUpdated": "2026-06-22T10:00:00+00:00"},
                "start": "2026-06-23T09:30:00+05:30",
                "minutesDuration": 20,
                "participant": [
                    {"actor": {"reference": "Patient/pat-1"}},
                    {"actor": {"reference": "Practitioner/doc-7"}},
                    {"actor": {"reference": "Location/dept-2"}},
                ],
            }
        }
    ],
}


def _adapter(handler):
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"}
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


@pytest.mark.asyncio
async def test_list_appointments_maps_fhir_to_canonical():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/Appointment")
        assert req.url.params.get("_lastUpdated", "").startswith("gt")
        return httpx.Response(200, json=BUNDLE)

    appts, cursor = await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))
    assert len(appts) == 1
    a = appts[0]
    assert a.appointment_id == "appt-1"
    assert a.hms_version == 3
    assert a.doctor_external_id == "doc-7"
    assert a.department_external_id == "dept-2"
    assert a.patient.mrn == "pat-1"
    assert a.slot_duration_min == 20
    assert a.status == "booked"
    assert cursor == "2026-06-22T10:00:00+00:00"  # max lastUpdated


@pytest.mark.asyncio
async def test_health_check_ok_on_metadata_200():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/metadata")
        return httpx.Response(200, json={"resourceType": "CapabilityStatement"})

    h = await _adapter(handler).health_check()
    assert h.healthy is True
    assert h.latency_ms is not None
