from datetime import date

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.exceptions import TransientError

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


@pytest.mark.asyncio
async def test_until_date_bounds_appointment_date_not_modification_time():
    """until_date is a clinic-day horizon on the appointment, not on _lastUpdated.

    Sending it as a lt bound on _lastUpdated excludes everything modified today,
    so the poller can never observe a same-day booking or cancellation. Verified
    against a live OpenEMR 8.3.0: the adapter's own query returned 0 rows for an
    appointment created that morning, and 1 row once the lt bound was dropped.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        lu = req.url.params.get_list("_lastUpdated")
        assert [p for p in lu if p.startswith("gt")], "expected a gt lower bound"
        assert not [p for p in lu if p.startswith("lt")], (
            f"_lastUpdated must carry no upper bound, got {lu}"
        )
        assert req.url.params.get_list("date") == ["le2026-06-30"], (
            f"until_date belongs on the appointment date param, got {req.url.params}"
        )
        return httpx.Response(200, json=BUNDLE)

    appts, _cursor = await _adapter(handler).list_appointments_modified_since(
        "2026-06-01T00:00:00+00:00", date(2026, 6, 30)
    )
    assert len(appts) == 1


def _appt(n: int) -> dict:
    return {
        "resource": {
            "resourceType": "Appointment",
            "id": f"appt-{n}",
            "status": "booked",
            "meta": {"versionId": "1", "lastUpdated": f"2026-06-22T10:{n % 60:02d}:00+00:00"},
            "start": "2026-06-23T09:30:00+05:30",
            "minutesDuration": 20,
            "participant": [{"actor": {"reference": "Patient/pat-1"}}],
        }
    }


@pytest.mark.asyncio
async def test_does_not_cap_the_result_set_with_count():
    """Asking for _count is what lets a non-paging server truncate us.

    OpenEMR 8.3.0 honours _count, reports `total` as the page size, and never
    emits link[relation=next] — and it ignores _offset, so there is no way to
    ask for the rest. Requesting 100 turns 132 matching rows into 100 with no
    signal. Omitting _count, the same server returns all of them.
    """
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(200, json=BUNDLE)

    await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))
    assert "_count" not in seen, f"_count invites silent truncation, got {seen}"


@pytest.mark.asyncio
async def test_bundle_reporting_more_than_it_returned_is_an_error():
    """total > entries with no next link means the server withheld records.

    Standards-based and detectable, unlike guessing from a page being 'full'.
    Advancing the cursor here would skip whatever was held back.
    """
    withheld = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 250,
        "entry": [_appt(i) for i in range(100)],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=withheld)

    with pytest.raises(TransientError, match="withheld"):
        await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))


@pytest.mark.asyncio
async def test_large_complete_result_is_not_an_error():
    """A big single-page answer is normal, not truncation.

    Guards the regression this fix caused first time round: keying the error off
    'page looks full' stalled the poller permanently against OpenEMR, which
    returns everything in one unpaginated bundle.
    """
    complete = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 100,
        "entry": [_appt(i) for i in range(100)],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=complete)

    appts, _ = await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))
    assert len(appts) == 100


@pytest.mark.asyncio
async def test_partial_page_without_next_link_is_the_normal_end():
    """Fewer entries than requested means the server really did send everything."""
    short_page = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 2,
        "entry": [_appt(i) for i in range(2)],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=short_page)

    appts, _ = await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))
    assert len(appts) == 2


@pytest.mark.asyncio
async def test_an_unparseable_slot_start_is_skipped_not_invented():
    """A bad timestamp must never become "now".

    Found against a hosted FHIR server carrying real-world messy data: two of
    2111 appointments had a malformed `start`, and the adapter substituted
    datetime.now(UTC). In a queue system that is worse than dropping the
    appointment — the patient is placed in today's queue at the current moment
    instead of their actual slot, and nothing downstream can tell the time was
    fabricated. Skip it loudly instead.
    """
    bad = {
        "resource": {
            "resourceType": "Appointment",
            "id": "appt-bad-start",
            "status": "booked",
            "meta": {"versionId": "1", "lastUpdated": "2026-06-22T10:00:00+00:00"},
            "start": "2024-11-25T15:30:00.000Z:00Z",  # verbatim from the live server
            "minutesDuration": 20,
            "participant": [{"actor": {"reference": "Patient/pat-1"}}],
        }
    }
    good = _appt(1)
    bundle = {"resourceType": "Bundle", "type": "searchset", "total": 2, "entry": [bad, good]}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bundle)

    appts, _ = await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))

    ids = [a.appointment_id for a in appts]
    assert "appt-bad-start" not in ids, (
        "an appointment with an unparseable time was returned anyway"
    )
    assert ids == ["appt-1"], f"the good appointment must still come through, got {ids}"


@pytest.mark.asyncio
async def test_a_missing_slot_start_is_also_skipped():
    """`start` absent entirely is the same problem, not a different one."""
    missing = {
        "resource": {
            "resourceType": "Appointment",
            "id": "appt-no-start",
            "status": "booked",
            "meta": {"versionId": "1", "lastUpdated": "2026-06-22T10:00:00+00:00"},
            "minutesDuration": 20,
            "participant": [{"actor": {"reference": "Patient/pat-1"}}],
        }
    }
    bundle = {"resourceType": "Bundle", "type": "searchset", "total": 1, "entry": [missing]}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bundle)

    appts, _ = await _adapter(handler).list_appointments_modified_since("", date(2026, 6, 30))
    assert appts == []
