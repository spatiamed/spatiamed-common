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


# --- New tests for review findings ---


@pytest.mark.asyncio
async def test_until_date_sent_as_lt_upper_bound():
    """until_date must be sent as a lt... _lastUpdated param alongside gt... (finding #1)."""

    def handler(req: httpx.Request) -> httpx.Response:
        lu_params = req.url.params.get_list("_lastUpdated")
        gt_params = [p for p in lu_params if p.startswith("gt")]
        lt_params = [p for p in lu_params if p.startswith("lt")]
        assert gt_params, "Expected a gt... _lastUpdated param"
        assert lt_params, "Expected a lt... _lastUpdated param (until_date upper bound)"
        assert lt_params[0] == "lt2026-06-30"
        return httpx.Response(200, json=BUNDLE)

    appts, _cursor = await _adapter(handler).list_appointments_modified_since(
        "2026-06-01T00:00:00+00:00", date(2026, 6, 30)
    )
    assert len(appts) == 1


@pytest.mark.asyncio
async def test_pagination_follows_next_link():
    """Adapter must follow Bundle next links until exhausted (finding #2).

    Page 1: entry with appt-1 + link[relation=next] → /Appointment?page=2
    Page 2: entry with appt-2, no next link
    Expect: both appointments returned, cursor = max lastUpdated across both pages.
    """
    page2_url = "https://hms.example/fhir/Appointment?page=2"

    bundle_page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "link": [
            {"relation": "self", "url": "https://hms.example/fhir/Appointment?page=1"},
            {"relation": "next", "url": page2_url},
        ],
        "entry": [
            {
                "resource": {
                    "resourceType": "Appointment",
                    "id": "appt-1",
                    "status": "booked",
                    "meta": {"versionId": "1", "lastUpdated": "2026-06-22T10:00:00+00:00"},
                    "start": "2026-06-23T09:00:00+05:30",
                    "minutesDuration": 15,
                    "participant": [
                        {"actor": {"reference": "Patient/pat-1"}},
                        {"actor": {"reference": "Practitioner/doc-1"}},
                        {"actor": {"reference": "Location/dept-1"}},
                    ],
                }
            }
        ],
    }

    bundle_page2 = {
        "resourceType": "Bundle",
        "type": "searchset",
        # No next link — last page
        "entry": [
            {
                "resource": {
                    "resourceType": "Appointment",
                    "id": "appt-2",
                    "status": "fulfilled",
                    "meta": {"versionId": "2", "lastUpdated": "2026-06-22T11:00:00+00:00"},
                    "start": "2026-06-23T10:00:00+05:30",
                    "minutesDuration": 30,
                    "participant": [
                        {"actor": {"reference": "Patient/pat-2"}},
                        {"actor": {"reference": "Practitioner/doc-2"}},
                        {"actor": {"reference": "Location/dept-2"}},
                    ],
                }
            }
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(200, json=bundle_page2)
        # First page — assert initial query params present
        assert req.url.path.endswith("/Appointment")
        return httpx.Response(200, json=bundle_page1)

    appts, cursor = await _adapter(handler).list_appointments_modified_since(
        "2026-06-01T00:00:00+00:00", date(2026, 6, 30)
    )

    ids = {a.appointment_id for a in appts}
    assert "appt-1" in ids, "appt-1 from page 1 must be included"
    assert "appt-2" in ids, "appt-2 from page 2 must be included"
    assert len(appts) == 2
    # cursor must be max lastUpdated across both pages
    assert cursor == "2026-06-22T11:00:00+00:00"
