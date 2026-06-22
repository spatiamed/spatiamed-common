"""FHIR R4 (ABDM) adapter — one schema for all FHIR-compliant HMS vendors.

Reads operational resources (Appointment, Patient, Practitioner) directly with
hospital-issued credentials. Never uses the ABDM consent gateway.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from uuid import UUID

import httpx

from sm_common.integrations.auth import build_auth_headers
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
from sm_common.integrations.hms_adapter import HmsAdapter


def _ref_id(participant_actor_ref: str) -> str:
    return participant_actor_ref.split("/")[-1] if participant_actor_ref else ""


class FhirR4Adapter(HmsAdapter):
    vendor_name = "fhir_r4"

    def __init__(
        self,
        *,
        base_url: str,
        auth_scheme: str,
        auth_cfg: dict,  # type: ignore[type-arg]
        hash_salt: str = "",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._scheme = auth_scheme
        self._cfg = auth_cfg
        self._hash_salt = hash_salt
        self._client = httpx.AsyncClient(timeout=30.0)

    async def _headers(self, body: str = "") -> dict[str, str]:
        return await build_auth_headers(self._client, self._scheme, self._cfg, body)

    def _actor(self, appt: dict, prefix: str) -> str:  # type: ignore[type-arg]
        for p in appt.get("participant", []):
            ref = p.get("actor", {}).get("reference", "")
            if ref.startswith(prefix + "/"):
                return _ref_id(ref)
        return ""

    def _to_canonical(self, appt: dict) -> CanonicalAppointment:  # type: ignore[type-arg]
        meta = appt.get("meta", {})
        start = appt.get("start", "")
        try:
            slot_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            slot_start = datetime.now(UTC)
        return CanonicalAppointment(
            appointment_id=str(appt.get("id", "")),
            hms_version=int(meta.get("versionId", 0) or 0),
            patient=CanonicalPatient(
                mrn=self._actor(appt, "Patient"),
                abha_id=None,
                phone_hash="",
                name_token="",
                age=None,
                gender=None,
            ),
            doctor_external_id=self._actor(appt, "Practitioner"),
            department_external_id=self._actor(appt, "Location"),
            slot_start=slot_start,
            slot_duration_min=int(appt.get("minutesDuration", 15) or 15),
            payer_type="CASH",
            reason_text=(appt.get("description") or None),
            status=str(appt.get("status", "")),
        )

    async def list_appointments_modified_since(
        self,
        cursor: str,
        until_date: date,
    ) -> tuple[list[CanonicalAppointment], str]:
        since = cursor or "1970-01-01T00:00:00+00:00"
        resp = await self._client.get(
            f"{self._base}/Appointment",
            params={"_lastUpdated": f"gt{since}", "_count": "100", "_sort": "_lastUpdated"},
            headers=await self._headers(),
        )
        resp.raise_for_status()
        bundle = resp.json()
        appts: list[CanonicalAppointment] = []
        new_cursor = cursor
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            if res.get("resourceType") != "Appointment":
                continue
            appts.append(self._to_canonical(res))
            lu = res.get("meta", {}).get("lastUpdated", "")
            if lu > new_cursor:
                new_cursor = lu
        return appts, new_cursor

    async def health_check(self) -> AdapterHealth:
        t0 = time.monotonic()
        try:
            resp = await self._client.get(
                f"{self._base}/metadata",
                headers=await self._headers(),
            )
            latency = int((time.monotonic() - t0) * 1000)
            ok = resp.status_code == 200
            return AdapterHealth(
                healthy=ok,
                last_success_at=datetime.now(UTC) if ok else None,
                latency_ms=latency,
                message="ok" if ok else f"metadata returned {resp.status_code}",
            )
        except httpx.HTTPError as exc:
            return AdapterHealth(
                healthy=False,
                last_success_at=None,
                latency_ms=None,
                message=str(exc),
            )

    # --- Implemented in Task 3 ---

    async def find_patient(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
    ) -> CanonicalPatient | None:
        raise NotImplementedError  # Task 3

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        raise NotImplementedError  # Task 3

    async def fetch_recent_bookings(
        self,
        hospital_id: UUID,
        lookback_minutes: int,
    ) -> list[ExternalBooking]:
        raise NotImplementedError  # Task 3

    async def write_back_idempotent(
        self,
        booking_id: UUID,
        payload: dict,  # type: ignore[type-arg]
        idempotency_key: str,
    ) -> WriteBackResult:
        raise NotImplementedError  # Task 3

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        raise NotImplementedError  # Task 3

    async def push_visit_event(
        self,
        event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> None:
        raise NotImplementedError  # Task 3
