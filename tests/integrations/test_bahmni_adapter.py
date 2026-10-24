import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import respx
import httpx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.canonical_types import (
    AdapterHealth,
    AppointmentWrite,
    VisitCheckedIn,
    WriteBackResult,
)
from sm_common.integrations.exceptions import ConflictError, TransientError


FIXTURE_DIR = Path(__file__).parent / "fixtures"

ATOM_XML = (FIXTURE_DIR / "bahmni_atomfeed.xml").read_text()


def _write(**over) -> AppointmentWrite:
    start = datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc)
    base = dict(
        booking_id=uuid4(),
        patient_ref="P1",
        practitioner_ref="DEPT-001",
        start=start,
        end=start + timedelta(minutes=15),
        reason="Fever",
    )
    base.update(over)
    return AppointmentWrite(**base)


BASE = "https://bahmni.example.com"
CONFIG = BahmniConfig(location_uuid="L", identifier_type_uuid="T", identifier_source_uuid="S")


def _login() -> None:
    """Every adapter call logs in first; serve a session for the test."""
    respx.get(f"{BASE}/openmrs/ws/rest/v1/session").mock(
        return_value=httpx.Response(
            200,
            json={"authenticated": True},
            headers={"set-cookie": "JSESSIONID=abc; Path=/openmrs"},
        )
    )


def make_adapter() -> BahmniAdapter:
    return BahmniAdapter(
        http=BahmniHttp(BASE, "user", "pw"),
        config=CONFIG,
        hash_salt="test-salt",
        transport_key="test-transport-key-32-char-padding!",
    )


class TestBahmniAdapterAtomFeed:
    @respx.mock
    async def test_list_appointments_returns_canonical(self):
        _login()
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
        _login()
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


class TestBahmniAdapterHealth:
    @respx.mock
    async def test_health_check_healthy(self):
        _login()
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
