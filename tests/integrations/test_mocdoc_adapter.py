import hashlib
import hmac
import json
import time
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
import respx
import httpx

from sm_common.integrations.adapters.mocdoc import MocDocAdapter
from sm_common.integrations.canonical_types import WriteBackResult
from sm_common.integrations.exceptions import ConflictError, TransientError


def make_adapter() -> MocDocAdapter:
    return MocDocAdapter(
        base_url="https://api.mocdoc.in",
        api_key="mocdoc-key-123",
        api_secret="mocdoc-secret-abc",
        hash_salt="test-salt",
        transport_key="test-transport-32-padding-chars!!",
    )


def _verify_hmac_header(request: httpx.Request, secret: str) -> bool:
    sig_header = request.headers.get("X-MocDoc-Signature", "")
    date_header = request.headers.get("Date", "")
    body = request.content.decode()
    expected = hmac.new(
        secret.encode(),
        f"{date_header}\n{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return sig_header == expected


class TestMocDocAdapterAuth:
    @respx.mock
    async def test_request_has_hmac_signature(self):
        route = respx.get("https://api.mocdoc.in/api/v1/appointments").mock(
            return_value=httpx.Response(200, json={"appointments": [], "cursor": "2026-05-06T10:00:00Z"})
        )
        adapter = make_adapter()
        await adapter.list_appointments_modified_since("", date(2026, 5, 8))
        assert route.called
        req = route.calls[0].request
        assert "X-MocDoc-Signature" in req.headers
        assert "Date" in req.headers

    @respx.mock
    async def test_hmac_signature_is_correct(self):
        route = respx.get("https://api.mocdoc.in/api/v1/appointments").mock(
            return_value=httpx.Response(200, json={"appointments": [], "cursor": "2026-05-06T10:00:00Z"})
        )
        adapter = make_adapter()
        await adapter.list_appointments_modified_since("", date(2026, 5, 8))
        req = route.calls[0].request
        assert _verify_hmac_header(req, "mocdoc-secret-abc")


class TestMocDocAdapterListAppointments:
    @respx.mock
    async def test_returns_canonical_appointments(self):
        respx.get("https://api.mocdoc.in/api/v1/appointments").mock(
            return_value=httpx.Response(200, json={
                "appointments": [{
                    "id": "APT-MD-001",
                    "version": 2,
                    "patientMrn": "MRN-MD-001",
                    "doctorId": "DR-MD-001",
                    "departmentId": "DEPT-MD-001",
                    "scheduledAt": "2026-05-07T10:00:00Z",
                    "durationMinutes": 15,
                    "payerType": "CASH",
                    "status": "confirmed",
                }],
                "cursor": "2026-05-07T10:00:00Z",
            })
        )
        adapter = make_adapter()
        apts, cursor = await adapter.list_appointments_modified_since("", date(2026, 5, 8))
        assert len(apts) == 1
        assert apts[0].appointment_id == "APT-MD-001"
        assert apts[0].doctor_external_id == "DR-MD-001"
        assert cursor == "2026-05-07T10:00:00Z"

    @respx.mock
    async def test_500_raises_transient(self):
        respx.get("https://api.mocdoc.in/api/v1/appointments").mock(
            return_value=httpx.Response(500)
        )
        adapter = make_adapter()
        with pytest.raises(TransientError):
            await adapter.list_appointments_modified_since("", date(2026, 5, 8))


class TestMocDocAdapterWriteBack:
    @respx.mock
    async def test_write_back_success(self):
        respx.post("https://api.mocdoc.in/api/v1/appointments").mock(
            return_value=httpx.Response(201, json={"id": "HB-MD-001", "status": "confirmed"})
        )
        adapter = make_adapter()
        result = await adapter.write_back_idempotent(uuid4(), {"patientId": "P1"}, "idem-001")
        assert result.status == "SUCCESS"
        assert result.hms_booking_id == "HB-MD-001"

    @respx.mock
    async def test_409_raises_conflict(self):
        respx.post("https://api.mocdoc.in/api/v1/appointments").mock(
            return_value=httpx.Response(409, json={"error": "slot already booked"})
        )
        adapter = make_adapter()
        with pytest.raises(ConflictError):
            await adapter.write_back_idempotent(uuid4(), {}, "idem-002")


class TestMocDocAdapterHealth:
    @respx.mock
    async def test_healthy(self):
        respx.get("https://api.mocdoc.in/api/v1/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        adapter = make_adapter()
        health = await adapter.health_check()
        assert health.healthy is True

    @respx.mock
    async def test_unhealthy_on_connect_error(self):
        respx.get("https://api.mocdoc.in/api/v1/health").mock(
            side_effect=httpx.ConnectError("refused")
        )
        adapter = make_adapter()
        health = await adapter.health_check()
        assert health.healthy is False
