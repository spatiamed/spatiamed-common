"""FHIR R4 (ABDM) adapter — one schema for all FHIR-compliant HMS vendors.

Reads operational resources (Appointment, Patient, Practitioner) directly with
hospital-issued credentials. Never uses the ABDM consent gateway.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


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

    async def close(self) -> None:
        await self._client.aclose()

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
            logger.warning(
                "FhirR4Adapter: could not parse slot start %r for appointment %s; "
                "falling back to datetime.now(UTC)",
                start,
                appt.get("id", "<unknown>"),
            )
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

    def _next_url(self, bundle: dict) -> str | None:  # type: ignore[type-arg]
        """Return the 'next' link URL from a FHIR searchset Bundle, or None."""
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                return str(link["url"])
        return None

    async def list_appointments_modified_since(
        self,
        cursor: str,
        until_date: date,
    ) -> tuple[list[CanonicalAppointment], str]:
        since = cursor or "1970-01-01T00:00:00+00:00"
        headers = await self._headers()

        # Initial page — pass both gt (lower bound) and lt (upper bound) for _lastUpdated
        resp = await self._client.get(
            f"{self._base}/Appointment",
            params={
                "_lastUpdated": [f"gt{since}", f"lt{until_date.isoformat()}"],
                "_count": "100",
                "_sort": "_lastUpdated",
            },
            headers=headers,
        )
        resp.raise_for_status()

        appts: list[CanonicalAppointment] = []
        new_cursor = cursor

        while True:
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                if res.get("resourceType") != "Appointment":
                    continue
                appts.append(self._to_canonical(res))
                lu = res.get("meta", {}).get("lastUpdated", "")
                if lu > new_cursor:
                    new_cursor = lu

            next_url = self._next_url(bundle)
            if not next_url:
                break

            # Fetch the next page using the server-supplied URL (auth headers re-attached)
            resp = await self._client.get(next_url, headers=headers)
            resp.raise_for_status()

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

    def _gender_code(self, fhir_gender: str | None) -> str | None:
        """Map FHIR gender string to canonical M/F/O."""
        return {"male": "M", "female": "F", "other": "O", "unknown": None}.get(fhir_gender or "")

    def _age_from_birthdate(self, birth_date: str | None) -> int | None:
        """Derive age in years from a FHIR birthDate string (YYYY-MM-DD)."""
        if not birth_date:
            return None
        try:
            born = date.fromisoformat(birth_date)
            today = datetime.now(UTC).date()
            return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except ValueError:
            return None

    def _patient_to_canonical(self, resource: dict) -> CanonicalPatient:  # type: ignore[type-arg]
        identifiers = resource.get("identifier", [])
        mrn = ""
        abha_id: str | None = None
        for ident in identifiers:
            system = ident.get("system", "")
            value = ident.get("value", "")
            if "ndhm" in system or "abha" in system.lower():
                abha_id = value
            else:
                if not mrn:
                    mrn = value

        names = resource.get("name", [])
        name_token = ""
        if names:
            name_token = names[0].get("text", "") or " ".join(
                filter(
                    None,
                    [
                        names[0].get("given", [""])[0] if names[0].get("given") else "",
                        names[0].get("family", ""),
                    ],
                )
            )

        phone_hash = ""
        for telecom in resource.get("telecom", []):
            if telecom.get("system") == "phone":
                phone_hash = telecom.get("value", "")
                break

        return CanonicalPatient(
            mrn=mrn,
            abha_id=abha_id,
            phone_hash=phone_hash,
            name_token=name_token,
            age=self._age_from_birthdate(resource.get("birthDate")),
            gender=self._gender_code(resource.get("gender")),  # type: ignore[arg-type]
        )

    async def find_patient(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
    ) -> CanonicalPatient | None:
        # Build the search param: prefer mrn, then abha_id, then phone_hash.
        # phone_hash is a hash, not a real phone — FHIR servers generally can't
        # search by it, so we return None when it is the only hint.
        if mrn:
            params: dict = {"identifier": mrn}  # type: ignore[type-arg]
        elif abha_id:
            params = {"identifier": abha_id}
        elif phone_hash:
            # phone_hash is not searchable; no way to query FHIR by it.
            return None
        else:
            return None

        try:
            resp = await self._client.get(
                f"{self._base}/Patient",
                params=params,
                headers=await self._headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.find_patient HTTP error: %s", exc)
            return None

        bundle = resp.json()
        entries = bundle.get("entry", [])
        if not entries:
            return None

        resource = entries[0].get("resource", {})
        return self._patient_to_canonical(resource)

    def _practitioner_to_canonical(self, resource: dict) -> CanonicalDoctor:  # type: ignore[type-arg]
        qualifications = resource.get("qualification", [])
        speciality_label = ""
        speciality_code = ""
        dept_code = ""
        dept_label = ""

        for qual in qualifications:
            code_obj = qual.get("code", {})
            codings = code_obj.get("coding", [])
            if codings:
                speciality_code = codings[0].get("code", "")
                speciality_label = codings[0].get("display", "")
            for ident in qual.get("identifier", []):
                if "dept" in ident.get("system", "").lower():
                    dept_code = ident.get("value", "")
                    dept_label = ident.get("display", dept_code)

        return CanonicalDoctor(
            external_doctor_id=str(resource.get("id", "")),
            external_speciality_id=speciality_code or "unknown",
            external_department_id=dept_code or "unknown",
            external_sub_dept_id=None,
            speciality_label=speciality_label or "Unknown",
            department_label=dept_label or "Unknown",
            consultation_fee_inr=None,
            consultation_duration_min=15,
            languages=[],
        )

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        headers = await self._headers()
        try:
            resp = await self._client.get(
                f"{self._base}/Practitioner",
                params={"_count": "200"},
                headers=headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.fetch_doctor_roster HTTP error: %s", exc)
            return []

        doctors: list[CanonicalDoctor] = []
        while True:
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") != "Practitioner":
                    continue
                doctors.append(self._practitioner_to_canonical(resource))

            next_url = self._next_url(bundle)
            if not next_url:
                break

            try:
                resp = await self._client.get(next_url, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("FhirR4Adapter.fetch_doctor_roster pagination error: %s", exc)
                break

        return doctors

    def _booking_to_external(self, resource: dict) -> ExternalBooking:  # type: ignore[type-arg]
        doctor_id = ""
        for p in resource.get("participant", []):
            ref = p.get("actor", {}).get("reference", "")
            if ref.startswith("Practitioner/"):
                doctor_id = _ref_id(ref)
                break

        start_str = resource.get("start", "")
        try:
            slot_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            slot_start = datetime.now(UTC)

        updated_str = resource.get("meta", {}).get("lastUpdated", "")
        try:
            updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            updated_at = datetime.now(UTC)

        return ExternalBooking(
            appointment_id=str(resource.get("id", "")),
            doctor_external_id=doctor_id,
            slot_start=slot_start,
            status=str(resource.get("status", "")),
            updated_at=updated_at,
        )

    async def fetch_recent_bookings(
        self,
        hospital_id: UUID,
        lookback_minutes: int,
    ) -> list[ExternalBooking]:
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
        headers = await self._headers()

        try:
            resp = await self._client.get(
                f"{self._base}/Appointment",
                params={"_lastUpdated": f"gt{since.isoformat()}"},
                headers=headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.fetch_recent_bookings HTTP error: %s", exc)
            return []

        bookings: list[ExternalBooking] = []
        while True:
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") != "Appointment":
                    continue
                bookings.append(self._booking_to_external(resource))

            next_url = self._next_url(bundle)
            if not next_url:
                break

            try:
                resp = await self._client.get(next_url, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("FhirR4Adapter.fetch_recent_bookings pagination error: %s", exc)
                break

        return bookings

    async def write_back_idempotent(
        self,
        booking_id: UUID,
        payload: dict,  # type: ignore[type-arg]
        idempotency_key: str,
    ) -> WriteBackResult:
        appt_id = payload.get("appointment_id") or str(booking_id)
        base_headers = await self._headers()
        headers = {**base_headers, "X-Idempotency-Key": idempotency_key}

        try:
            resp = await self._client.put(
                f"{self._base}/Appointment/{appt_id}",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.write_back_idempotent HTTP error: %s", exc)
            return WriteBackResult(status="TRANSIENT_ERROR", error_detail=str(exc))

        if resp.status_code in (200, 201):
            body = resp.json()
            return WriteBackResult(
                status="SUCCESS",
                hms_booking_id=str(body.get("id", appt_id)),
            )
        if resp.status_code == 409:
            return WriteBackResult(status="CONFLICT", error_detail=resp.text)

        # 4xx (other) or 5xx
        return WriteBackResult(
            status="TRANSIENT_ERROR",
            error_detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        base_headers = await self._headers()
        body = {
            "resourceType": "Appointment",
            "id": hms_booking_id,
            "status": "cancelled",
            "cancelationReason": {
                "text": reason,
            },
        }

        try:
            resp = await self._client.put(
                f"{self._base}/Appointment/{hms_booking_id}",
                json=body,
                headers=base_headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.cancel HTTP error: %s", exc)
            return CancelResult(status="FAILED", error_detail=str(exc))

        if resp.status_code == 200:
            return CancelResult(status="SUCCESS")
        if resp.status_code == 404:
            return CancelResult(status="NOT_FOUND", error_detail=resp.text)

        return CancelResult(
            status="FAILED",
            error_detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )

    def _visit_event_status(
        self,
        event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> str:
        if isinstance(event, VisitCheckedIn):
            return "arrived"
        if isinstance(event, VisitConsultationStarted):
            return "in-progress"
        if isinstance(event, VisitFinalized):
            status_map = {
                "completed": "finished",
                "no_show": "dnf",
                "cancelled_after_arrival": "cancelled",
            }
            return status_map.get(event.final_status, "finished")
        return "unknown"

    async def push_visit_event(
        self,
        event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> None:
        base_headers = await self._headers()
        headers = {**base_headers, "X-Idempotency-Key": str(event.event_uuid)}

        encounter_body = {
            "resourceType": "Encounter",
            "status": self._visit_event_status(event),
            "identifier": [{"value": str(event.event_uuid)}],
        }

        try:
            resp = await self._client.post(
                f"{self._base}/Encounter",
                json=encounter_body,
                headers=headers,
            )
            if resp.status_code >= 300:
                logger.warning(
                    "FhirR4Adapter.push_visit_event non-2xx response %s for event %s",
                    resp.status_code,
                    event.event_uuid,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "FhirR4Adapter.push_visit_event HTTP error for event %s: %s",
                event.event_uuid,
                exc,
            )
