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
    AdapterHealth, CancelResult, CanonicalAppointment, CanonicalDoctor,
    CanonicalPatient, ExternalBooking, VisitCheckedIn,
    VisitConsultationStarted, VisitFinalized, WriteBackResult,
)
from sm_common.integrations.exceptions import AuthError, ConflictError, TransientError
from sm_common.integrations.hms_adapter import HmsAdapter


class GenericRestAdapter(HmsAdapter):
    """Configurable REST adapter for Tier 2 HMS vendors without bespoke implementations.

    Driven by a field_mapping dict. Supports api_key and hmac auth schemes.
    Handles MediXcel, DocPulse, Halemind, Ezovion and similar REST APIs.
    """

    vendor_name = "generic_rest"

    def __init__(self, field_mapping: dict) -> None:  # type: ignore[type-arg]
        self._m = field_mapping
        self._client = httpx.AsyncClient(timeout=30.0)

    def _auth_headers(self, body: str = "") -> dict[str, str]:
        scheme = self._m.get("auth_scheme", "api_key")
        if scheme == "api_key":
            header_name = self._m.get("api_key_header", "X-Api-Key")
            return {header_name: self._m.get("api_key", ""), "Content-Type": "application/json"}
        if scheme == "hmac":
            date_str = formatdate(usegmt=True)
            secret = self._m.get("api_secret", "")
            sig = hmac.new(secret.encode(), f"{date_str}\n{body}".encode(), hashlib.sha256).hexdigest()
            return {
                self._m.get("api_key_header", "X-Api-Key"): self._m.get("api_key", ""),
                "X-Signature": sig, "Date": date_str, "Content-Type": "application/json",
            }
        return {"Content-Type": "application/json"}

    def _get_field(self, obj: dict, canonical: str) -> object:  # type: ignore[type-arg]
        vendor_key = self._m.get("appointment_fields", {}).get(canonical, canonical)
        return obj.get(vendor_key)

    def _to_canonical(self, apt: dict) -> CanonicalAppointment:  # type: ignore[type-arg]
        slot_str = str(self._get_field(apt, "slot_start") or "")
        try:
            slot_start = datetime.fromisoformat(slot_str.replace("Z", "+00:00"))
        except ValueError:
            slot_start = datetime.now(timezone.utc)
        patient = CanonicalPatient(
            mrn=str(self._get_field(apt, "mrn") or ""),
            abha_id=None, phone_hash="", name_token="", age=None, gender=None,
        )
        duration = self._get_field(apt, "slot_duration_min")
        version = self._get_field(apt, "hms_version")
        return CanonicalAppointment(
            appointment_id=str(self._get_field(apt, "appointment_id") or ""),
            hms_version=int(version) if version else 0,
            patient=patient,
            doctor_external_id=str(self._get_field(apt, "doctor_external_id") or ""),
            department_external_id=str(self._get_field(apt, "department_external_id") or ""),
            slot_start=slot_start,
            slot_duration_min=int(duration) if duration else 15,
            payer_type=str(self._get_field(apt, "payer_type") or "CASH"),
            reason_text=None,
            status=str(self._get_field(apt, "status") or ""),
        )

    async def list_appointments_modified_since(
        self, cursor: str, until_date: date
    ) -> tuple[list[CanonicalAppointment], str]:
        params: dict[str, str] = {}
        if cursor:
            params["modified_since"] = cursor
        resp = await self._client.get(
            f"{self._m['base_url']}{self._m['list_appointments_path']}",
            headers=self._auth_headers(), params=params,
        )
        if resp.status_code in (401, 403):
            raise AuthError(f"GenericRest auth failed: {resp.status_code}")
        if resp.status_code >= 500:
            raise TransientError(f"GenericRest server error: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        list_key = self._m.get("appointments_list_key", "appointments")
        apts = [self._to_canonical(a) for a in data.get(list_key, [])]
        cursor_field = self._m.get("cursor_field", "cursor")
        return apts, data.get(cursor_field, cursor)

    async def find_patient(
        self, phone_hash: str | None = None,
        mrn: str | None = None, abha_id: str | None = None,
    ) -> CanonicalPatient | None:
        path = self._m.get("patient_lookup_path", "/patients")
        param_name = self._m.get("patient_lookup_param", "mrn")
        param_val = mrn or phone_hash or ""
        resp = await self._client.get(
            f"{self._m['base_url']}{path}",
            headers=self._auth_headers(), params={param_name: param_val},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise TransientError(f"GenericRest error: {resp.status_code}")
        resp.raise_for_status()
        patients = resp.json().get("patients", [])
        if not patients:
            return None
        p = patients[0]
        return CanonicalPatient(
            mrn=p.get("mrn", ""), abha_id=None,
            phone_hash=phone_hash or "", name_token="", age=None, gender=None,
        )

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        path = self._m.get("doctor_roster_path", "/doctors")
        resp = await self._client.get(
            f"{self._m['base_url']}{path}", headers=self._auth_headers(),
        )
        if resp.status_code >= 500:
            raise TransientError(f"GenericRest error: {resp.status_code}")
        resp.raise_for_status()
        list_key = self._m.get("doctors_list_key", "doctors")
        df = self._m.get("doctor_fields", {})

        def _df(d: dict, canon: str) -> str:  # type: ignore[type-arg]
            return str(d.get(df.get(canon, canon), ""))

        return [
            CanonicalDoctor(
                external_doctor_id=_df(d, "external_doctor_id"),
                external_speciality_id=_df(d, "external_speciality_id"),
                external_department_id=_df(d, "external_department_id"),
                external_sub_dept_id=None,
                speciality_label=_df(d, "speciality_label"),
                department_label=_df(d, "department_label"),
                consultation_fee_inr=None,
                consultation_duration_min=int(
                    d.get(df.get("consultation_duration_min", "consultDuration"), 15) or 15
                ),
            )
            for d in resp.json().get(list_key, [])
        ]

    async def fetch_recent_bookings(
        self, hospital_id: UUID, lookback_minutes: int
    ) -> list[ExternalBooking]:
        path = self._m.get("list_appointments_path", "/appointments")
        resp = await self._client.get(
            f"{self._m['base_url']}{path}",
            headers=self._auth_headers(),
            params={"lookback_minutes": str(lookback_minutes)},
        )
        if resp.status_code >= 500:
            raise TransientError(f"GenericRest error: {resp.status_code}")
        resp.raise_for_status()
        list_key = self._m.get("appointments_list_key", "appointments")
        bookings = []
        for b in resp.json().get(list_key, []):
            try:
                slot_str = str(self._get_field(b, "slot_start") or "")
                slot = datetime.fromisoformat(slot_str.replace("Z", "+00:00"))
                updated_at = datetime.now(timezone.utc)
            except ValueError:
                continue
            bookings.append(ExternalBooking(
                appointment_id=str(self._get_field(b, "appointment_id") or ""),
                doctor_external_id=str(self._get_field(b, "doctor_external_id") or ""),
                slot_start=slot,
                status=str(self._get_field(b, "status") or ""),
                updated_at=updated_at,
            ))
        return bookings

    async def write_back_idempotent(
        self, booking_id: UUID, payload: dict, idempotency_key: str  # type: ignore[type-arg]
    ) -> WriteBackResult:
        idem_field = self._m.get("idempotency_field", "idempotencyKey")
        body = {**payload, idem_field: idempotency_key}
        body_str = _json.dumps(body, separators=(",", ":"))
        path = self._m.get("write_back_path", "/appointments")
        conflict_code = self._m.get("conflict_status_code", 409)
        resp = await self._client.post(
            f"{self._m['base_url']}{path}",
            headers=self._auth_headers(body_str), content=body_str,
        )
        if resp.status_code == conflict_code:
            raise ConflictError(f"conflict: HTTP {resp.status_code}")
        if resp.status_code >= 500:
            raise TransientError(f"GenericRest server error: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        apt_id_field = self._m.get("appointment_fields", {}).get("appointment_id", "id")
        return WriteBackResult(status="SUCCESS", hms_booking_id=data.get(apt_id_field))

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        path = self._m.get("write_back_path", "/appointments")
        resp = await self._client.delete(
            f"{self._m['base_url']}{path}/{hms_booking_id}",
            headers=self._auth_headers(), params={"reason": reason},
        )
        if resp.status_code == 404:
            return CancelResult(status="NOT_FOUND")
        if resp.status_code >= 500:
            return CancelResult(status="FAILED", error_detail=f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return CancelResult(status="SUCCESS")

    async def push_visit_event(
        self, event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> None:
        if isinstance(event, VisitCheckedIn):
            status = "arrived"
        elif isinstance(event, VisitConsultationStarted):
            status = "in_consultation"
        else:
            status = event.final_status
        body = {"status": status, "idempotencyKey": str(event.event_uuid)}
        body_str = _json.dumps(body, separators=(",", ":"))
        path = self._m.get("write_back_path", "/appointments")
        resp = await self._client.patch(
            f"{self._m['base_url']}{path}/{event.appointment_id}/status",
            headers=self._auth_headers(body_str), content=body_str,
        )
        if resp.status_code >= 500:
            raise TransientError(f"GenericRest error: {resp.status_code}")
        resp.raise_for_status()

    async def health_check(self) -> AdapterHealth:
        health_path = self._m.get("health_path", "/health")
        start = time.monotonic()
        try:
            resp = await self._client.get(
                f"{self._m['base_url']}{health_path}", headers=self._auth_headers(),
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code < 300:
                return AdapterHealth(
                    healthy=True, last_success_at=datetime.now(timezone.utc),
                    latency_ms=latency_ms, message="OK",
                )
            return AdapterHealth(
                healthy=False, last_success_at=None,
                latency_ms=latency_ms, message=f"HTTP {resp.status_code}",
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return AdapterHealth(
                healthy=False, last_success_at=None,
                latency_ms=None, message=f"connection error: {exc}",
            )
