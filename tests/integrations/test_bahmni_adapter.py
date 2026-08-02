import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import respx
import httpx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.canonical_types import (
    AdapterHealth,
    VisitCheckedIn,
    WriteBackResult,
)
from sm_common.integrations.exceptions import ConflictError, TransientError


FIXTURE_DIR = Path(__file__).parent / "fixtures"

ATOM_XML = (FIXTURE_DIR / "bahmni_atomfeed.xml").read_text()


def make_adapter(auth: str = "api_key") -> BahmniAdapter:
    return BahmniAdapter(
        base_url="https://bahmni.example.com",
        auth_scheme=auth,
        api_key="test-key-123",
        hash_salt="test-salt",
        transport_key="test-transport-key-32-char-padding!",
    )


class TestBahmniAdapterAtomFeed:
    @respx.mock
    async def test_list_appointments_returns_canonical(self):
        respx.get("https://bahmni.example.com/openmrs/ws/atomfeed/appointment/recent").mock(
            return_value=httpx.Response(
                200, text=ATOM_XML, headers={"Content-Type": "application/atom+xml"}
            )
        )
        adapter = make_adapter()
        appointments, new_cursor = await adapter.list_appointments_modified_since(
            cursor="", until_date=date(2026, 5, 8)
        )
        assert len(appointments) == 1
        assert appointments[0].appointment_id == "APT-001"
        assert appointments[0].doctor_external_id == "DR-001"
        assert appointments[0].department_external_id == "DEPT-001"
        assert new_cursor == "2026-05-06T10:00:00Z"

    @respx.mock
    async def test_list_appointments_empty_feed(self):
        empty_xml = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <updated>2026-05-06T10:00:00Z</updated>
        </feed>"""
        respx.get("https://bahmni.example.com/openmrs/ws/atomfeed/appointment/recent").mock(
            return_value=httpx.Response(200, text=empty_xml)
        )
        adapter = make_adapter()
        appointments, cursor = await adapter.list_appointments_modified_since("", date(2026, 5, 8))
        assert appointments == []


class TestBahmniAdapterWriteBack:
    @respx.mock
    async def test_write_back_success(self):
        respx.post("https://bahmni.example.com/openmrs/ws/rest/v1/appointment").mock(
            return_value=httpx.Response(
                200, json={"uuid": "HB-001", "appointmentNumber": "APT-002"}
            )
        )
        adapter = make_adapter()
        result = await adapter.write_back_idempotent(
            booking_id=uuid4(),
            payload={"patientUuid": "P1", "serviceUuid": "DEPT-001"},
            idempotency_key="idem-001",
        )
        assert result.status == "SUCCESS"
        assert result.hms_booking_id == "HB-001"

    @respx.mock
    async def test_write_back_conflict_raises(self):
        respx.post("https://bahmni.example.com/openmrs/ws/rest/v1/appointment").mock(
            return_value=httpx.Response(
                400, json={"errorMessages": [{"message": "slot already booked for this time"}]}
            )
        )
        adapter = make_adapter()
        with pytest.raises(ConflictError):
            await adapter.write_back_idempotent(uuid4(), {}, "idem-002")

    @respx.mock
    async def test_write_back_500_raises_transient(self):
        respx.post("https://bahmni.example.com/openmrs/ws/rest/v1/appointment").mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )
        adapter = make_adapter()
        with pytest.raises(TransientError):
            await adapter.write_back_idempotent(uuid4(), {}, "idem-003")


class TestBahmniAdapterHealth:
    @respx.mock
    async def test_health_check_healthy(self):
        respx.get("https://bahmni.example.com/openmrs/ws/rest/v1/session").mock(
            return_value=httpx.Response(200, json={"authenticated": True})
        )
        adapter = make_adapter()
        health = await adapter.health_check()
        assert health.healthy is True

    @respx.mock
    async def test_health_check_unhealthy_on_connection_error(self):
        respx.get("https://bahmni.example.com/openmrs/ws/rest/v1/session").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        adapter = make_adapter()
        health = await adapter.health_check()
        assert health.healthy is False
        assert "connection" in health.message.lower()
