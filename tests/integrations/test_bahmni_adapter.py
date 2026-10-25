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
