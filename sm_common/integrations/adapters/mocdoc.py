from __future__ import annotations

import hashlib
import hmac
import json as _json
import time
from datetime import date, datetime, timezone
from email.utils import formatdate
from uuid import UUID

import httpx

from sm_common.integrations.canonical_types import (
    AdapterHealth,
    CancelResult,
    CanonicalAppointment,
    CanonicalDoctor,
    CanonicalPatient,
    ExternalBooking,
    VisitCheckedIn,
    VisitConsultationStarted,
    VisitFinalized,
    WriteBackResult,
)
from sm_common.integrations.exceptions import AuthError, ConflictError, TransientError
from sm_common.integrations.hms_adapter import HmsAdapter


class MocDocAdapter(HmsAdapter):
    """HMS adapter for MocDoc REST API (mocdoc.com).

    Auth: HMAC-SHA256 — Date header + body → X-MocDoc-Signature.
    Inbound: GET /api/v1/appointments?modified_since=<cursor>&limit=100
    Write-back: POST /api/v1/appointments with Idempotency-Key header.
    """

    vendor_name = "mocdoc"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        hash_salt: str,
        transport_key: str,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._hash_salt = hash_salt
        self._transport_key = transport_key
        self._client = httpx.AsyncClient(timeout=30.0)

    def _sign(self, date_str: str, body: str) -> str:
        message = f"{date_str}\n{body}"
        return hmac.new(
            self._api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, body: str = "") -> dict[str, str]:
        date_str = formatdate(usegmt=True)
        sig = self._sign(date_str, body)
        return {
            "X-MocDoc-ApiKey": self._api_key,
            "X-MocDoc-Signature": sig,
            "Date": date_str,
            "Content-Type": "application/json",
        }

    def _to_canonical(self, apt: dict) -> CanonicalAppointment:  # type: ignore[type-arg]
        try:
            slot_start = datetime.fromisoformat(apt["scheduledAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            slot_start = datetime.now(timezone.utc)
        patient = CanonicalPatient(
            mrn=apt.get("patientMrn", ""),
            abha_id=apt.get("abhaId"),
            phone_hash=apt.get("phoneHash", ""),
            name_token=apt.get("patientNameToken", ""),
            age=apt.get("patientAge"),
            gender=apt.get("patientGender"),
        )
        return CanonicalAppointment(
            appointment_id=apt.get("id", ""),
            hms_version=apt.get("version", 0),
            patient=patient,
            doctor_external_id=apt.get("doctorId", ""),
            department_external_id=apt.get("departmentId", ""),
            slot_start=slot_start,
            slot_duration_min=apt.get("durationMinutes", 15),
            payer_type=apt.get("payerType", "CASH"),
            reason_text=apt.get("reason"),
            status=apt.get("status", ""),
        )

    async def list_appointments_modified_since(
        self, cursor: str, until_date: date
    ) -> tuple[list[CanonicalAppointment], str]:
        headers = self._auth_headers()
        params: dict[str, str] = {"limit": "100"}
        if cursor:
            params["modified_since"] = cursor
        resp = await self._client.get(
            f"{self._base_url}/api/v1/appointments",
            headers=headers,
            params=params,
        )
        if resp.status_code in (401, 403):
            raise AuthError(f"MocDoc auth failed: {resp.status_code}")
        if resp.status_code >= 500:
            raise TransientError(f"MocDoc server error: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        apts = [self._to_canonical(a) for a in data.get("appointments", [])]
        new_cursor = data.get("cursor", cursor)
        return apts, new_cursor

    async def find_patient(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
    ) -> CanonicalPatient | None:
        params: dict[str, str] = {}
        if phone_hash:
            params["phone_hash"] = phone_hash
        elif mrn:
            params["mrn"] = mrn
        resp = await self._client.get(
            f"{self._base_url}/api/v1/patients",
            headers=self._auth_headers(),
            params=params,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise TransientError(f"MocDoc error: {resp.status_code}")
        resp.raise_for_status()
        results = resp.json().get("patients", [])
        if not results:
            return None
        p = results[0]
        return CanonicalPatient(
            mrn=p.get("mrn", ""),
            abha_id=p.get("abhaId"),
            phone_hash=phone_hash or "",
            name_token=p.get("nameToken", ""),
            age=p.get("age"),
            gender=p.get("gender"),
        )

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        resp = await self._client.get(
            f"{self._base_url}/api/v1/doctors",
            headers=self._auth_headers(),
        )
        if resp.status_code >= 500:
            raise TransientError(f"MocDoc error: {resp.status_code}")
        resp.raise_for_status()
        return [
            CanonicalDoctor(
                external_doctor_id=d.get("id", ""),
                external_speciality_id=d.get("specialityId", ""),
                external_department_id=d.get("departmentId", ""),
                external_sub_dept_id=d.get("subDeptId"),
                speciality_label=d.get("speciality", ""),
                department_label=d.get("department", ""),
                consultation_fee_inr=d.get("consultationFee"),
                consultation_duration_min=d.get("durationMinutes", 15),
                languages=d.get("languages", []),
            )
            for d in resp.json().get("doctors", [])
        ]

    async def fetch_recent_bookings(
        self, hospital_id: UUID, lookback_minutes: int
    ) -> list[ExternalBooking]:
        resp = await self._client.get(
            f"{self._base_url}/api/v1/appointments",
            headers=self._auth_headers(),
            params={"lookback_minutes": str(lookback_minutes)},
        )
        if resp.status_code >= 500:
            raise TransientError(f"MocDoc error: {resp.status_code}")
        resp.raise_for_status()
        bookings = []
        for b in resp.json().get("appointments", []):
            try:
                slot = datetime.fromisoformat(b["scheduledAt"].replace("Z", "+00:00"))
                updated = datetime.fromisoformat(
                    b.get("updatedAt", b["scheduledAt"]).replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                continue
            bookings.append(
                ExternalBooking(
                    appointment_id=b.get("id", ""),
                    doctor_external_id=b.get("doctorId", ""),
                    slot_start=slot,
                    status=b.get("status", ""),
                    updated_at=updated,
                )
            )
        return bookings

    async def write_back_idempotent(
        self,
        booking_id: UUID,
        payload: dict,
        idempotency_key: str,  # type: ignore[type-arg]
    ) -> WriteBackResult:
        body_str = _json.dumps(payload, separators=(",", ":"))
        headers = {**self._auth_headers(body_str), "Idempotency-Key": idempotency_key}
        resp = await self._client.post(
            f"{self._base_url}/api/v1/appointments",
            headers=headers,
            content=body_str,
        )
        if resp.status_code == 409:
            raise ConflictError(resp.json().get("error", "conflict"))
        if resp.status_code >= 500:
            raise TransientError(f"MocDoc server error: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        return WriteBackResult(status="SUCCESS", hms_booking_id=data.get("id"))

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        resp = await self._client.delete(
            f"{self._base_url}/api/v1/appointments/{hms_booking_id}",
            headers=self._auth_headers(),
            params={"reason": reason},
        )
        if resp.status_code == 404:
            return CancelResult(status="NOT_FOUND")
        if resp.status_code >= 500:
            return CancelResult(status="FAILED", error_detail=f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return CancelResult(status="SUCCESS")

    async def push_visit_event(
        self, event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized
    ) -> None:
        if isinstance(event, VisitCheckedIn):
            payload = {"status": "arrived", "idempotencyKey": str(event.event_uuid)}
        elif isinstance(event, VisitConsultationStarted):
            payload = {"status": "in_consultation", "idempotencyKey": str(event.event_uuid)}
        else:
            payload = {"status": event.final_status, "idempotencyKey": str(event.event_uuid)}
        body_str = _json.dumps(payload, separators=(",", ":"))
        resp = await self._client.patch(
            f"{self._base_url}/api/v1/appointments/{event.appointment_id}/status",
            headers=self._auth_headers(body_str),
            content=body_str,
        )
        if resp.status_code >= 500:
            raise TransientError(f"MocDoc error: {resp.status_code}")
        resp.raise_for_status()

    async def health_check(self) -> AdapterHealth:
        start = time.monotonic()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v1/health",
                headers=self._auth_headers(),
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code == 200:
                return AdapterHealth(
                    healthy=True,
                    last_success_at=datetime.now(timezone.utc),
                    latency_ms=latency_ms,
                    message="OK",
                )
            return AdapterHealth(
                healthy=False,
                last_success_at=None,
                latency_ms=latency_ms,
                message=f"HTTP {resp.status_code}",
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return AdapterHealth(
                healthy=False,
                last_success_at=None,
                latency_ms=None,
                message=f"connection error: {exc}",
            )
