from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

import feedparser
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


class BahmniAdapter(HmsAdapter):
    """HMS adapter for Bahmni/OpenMRS deployments.

    Inbound: AtomFeed polling at /openmrs/ws/atomfeed/appointment/recent
    Outbound: REST POST to /openmrs/ws/rest/v1/appointment
    Auth: api_key (Authorization: Bearer) or session (POST /session, token cached)
    """

    vendor_name = "bahmni"

    def __init__(
        self,
        base_url: str,
        auth_scheme: str,  # "api_key" | "session"
        hash_salt: str,
        transport_key: str,
        api_key: str = "",
        username: str = "",
        password: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_scheme = auth_scheme
        self._api_key = api_key
        self._username = username
        self._password = password
        self._hash_salt = hash_salt
        self._transport_key = transport_key
        self._session_token: str | None = None
        self._client = httpx.AsyncClient(timeout=30.0)

    async def _get_headers(self) -> dict[str, str]:
        if self._auth_scheme == "api_key":
            return {"Authorization": f"Bearer {self._api_key}"}
        return {"Cookie": f"JSESSIONID={await self._get_session_token()}"}

    async def _get_session_token(self) -> str:
        if self._session_token:
            return self._session_token
        resp = await self._client.post(
            f"{self._base_url}/openmrs/ws/rest/v1/session",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code == 401:
            raise AuthError("Bahmni session auth failed — check username/password")
        resp.raise_for_status()
        self._session_token = resp.json().get("sessionId", "")
        return self._session_token

    async def _invalidate_session(self) -> None:
        self._session_token = None

    def _parse_atom_entry(self, entry: Any) -> CanonicalAppointment | None:
        content = getattr(entry, "content", [])
        if not content:
            return None
        xml_str = content[0].get("value", "")
        if not xml_str:
            return None
        try:
            root = ET.fromstring(xml_str.strip())
        except ET.ParseError:
            return None

        def _text(tag: str) -> str:
            el = root.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        slot_str = _text("startDateTime")
        try:
            slot_start = datetime.fromisoformat(slot_str.replace("Z", "+00:00"))
        except ValueError:
            return None

        patient_el = root.find("patient")
        mrn = ""
        if patient_el is not None:
            id_el = patient_el.find("identifier")
            mrn = id_el.text.strip() if id_el is not None and id_el.text else ""

        provider_el = root.find("provider")
        doctor_id = ""
        if provider_el is not None:
            u = provider_el.find("uuid")
            doctor_id = u.text.strip() if u is not None and u.text else ""

        service_el = root.find("service")
        dept_id = ""
        if service_el is not None:
            u = service_el.find("uuid")
            dept_id = u.text.strip() if u is not None and u.text else ""

        apt_num = _text("appointmentNumber")
        try:
            hms_version = int(_text("version") or "0")
        except ValueError:
            hms_version = 0

        patient = CanonicalPatient(
            mrn=mrn,
            abha_id=None,
            phone_hash="",
            name_token="",
            age=None,
            gender=None,
        )
        return CanonicalAppointment(
            appointment_id=apt_num,
            hms_version=hms_version,
            patient=patient,
            doctor_external_id=doctor_id,
            department_external_id=dept_id,
            slot_start=slot_start,
            slot_duration_min=15,
            payer_type="CASH",
            reason_text=None,
            status=_text("status") or "Scheduled",
        )

    async def list_appointments_modified_since(
        self, cursor: str, until_date: date
    ) -> tuple[list[CanonicalAppointment], str]:
        url = f"{self._base_url}/openmrs/ws/atomfeed/appointment/recent"
        headers = await self._get_headers()
        resp = await self._client.get(url, headers=headers)
        if resp.status_code in (401, 403):
            await self._invalidate_session()
            raise AuthError(f"Bahmni auth failed: {resp.status_code}")
        if resp.status_code >= 500:
            raise TransientError(f"Bahmni server error: {resp.status_code}")
        resp.raise_for_status()

        feed = feedparser.parse(resp.text)
        new_cursor: str = getattr(feed.feed, "updated", cursor) or cursor
        appointments = []
        for entry in feed.entries:
            apt = self._parse_atom_entry(entry)
            if apt is not None:
                appointments.append(apt)
        return appointments, new_cursor

    async def find_patient(
        self, phone_hash: str | None = None, mrn: str | None = None, abha_id: str | None = None
    ) -> CanonicalPatient | None:
        headers = await self._get_headers()
        params: dict[str, str] = {}
        if mrn:
            params["identifier"] = mrn
        elif phone_hash:
            params["phoneHash"] = phone_hash
        resp = await self._client.get(
            f"{self._base_url}/openmrs/ws/rest/v1/patient",
            headers=headers,
            params=params,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise TransientError(f"Bahmni error: {resp.status_code}")
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        p = results[0]
        return CanonicalPatient(
            mrn=p.get("identifiers", [{}])[0].get("identifier", ""),
            abha_id=None,
            phone_hash=phone_hash or "",
            name_token="",
            age=None,
            gender=None,
        )

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        headers = await self._get_headers()
        resp = await self._client.get(
            f"{self._base_url}/openmrs/ws/rest/v1/provider",
            headers=headers,
            params={"v": "full"},
        )
        if resp.status_code >= 500:
            raise TransientError(f"Bahmni error: {resp.status_code}")
        resp.raise_for_status()
        doctors = []
        for p in resp.json().get("results", []):
            attrs = {a["attributeType"]["display"]: a["value"] for a in p.get("attributes", [])}
            doctors.append(
                CanonicalDoctor(
                    external_doctor_id=p.get("uuid", ""),
                    external_speciality_id=attrs.get("specialityUuid", ""),
                    external_department_id=attrs.get("departmentUuid", ""),
                    external_sub_dept_id=None,
                    speciality_label=attrs.get("speciality", ""),
                    department_label=attrs.get("department", ""),
                    consultation_fee_inr=None,
                    consultation_duration_min=15,
                )
            )
        return doctors

    async def fetch_recent_bookings(
        self, hospital_id: UUID, lookback_minutes: int
    ) -> list[ExternalBooking]:
        headers = await self._get_headers()
        resp = await self._client.get(
            f"{self._base_url}/openmrs/ws/rest/v1/appointment",
            headers=headers,
            params={"startDate": datetime.now(timezone.utc).isoformat(), "v": "full"},
        )
        if resp.status_code >= 500:
            raise TransientError(f"Bahmni error: {resp.status_code}")
        resp.raise_for_status()
        bookings = []
        for b in resp.json().get("results", []):
            try:
                slot_start = datetime.fromisoformat(b["startDateTime"].replace("Z", "+00:00"))
                updated_at = datetime.fromisoformat(
                    b.get("dateChanged", b["startDateTime"]).replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                continue
            bookings.append(
                ExternalBooking(
                    appointment_id=b.get("appointmentNumber", ""),
                    doctor_external_id=(b.get("provider") or {}).get("uuid", ""),
                    slot_start=slot_start,
                    status=b.get("status", ""),
                    updated_at=updated_at,
                )
            )
        return bookings

    async def write_back_idempotent(
        self,
        booking_id: UUID,
        payload: dict,
        idempotency_key: str,  # type: ignore[type-arg]
    ) -> WriteBackResult:
        headers = await self._get_headers()
        body = {**payload, "externalReference": idempotency_key}
        resp = await self._client.post(
            f"{self._base_url}/openmrs/ws/rest/v1/appointment",
            headers=headers,
            json=body,
        )
        if resp.status_code == 400:
            data = resp.json()
            msgs = [m.get("message", "") for m in data.get("errorMessages", [])]
            if any("slot" in m.lower() for m in msgs):
                raise ConflictError("; ".join(msgs))
            raise TransientError(f"Bahmni 400: {msgs}")
        if resp.status_code >= 500:
            raise TransientError(f"Bahmni server error: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        return WriteBackResult(
            status="SUCCESS",
            hms_booking_id=data.get("uuid") or data.get("appointmentNumber"),
        )

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        headers = await self._get_headers()
        resp = await self._client.post(
            f"{self._base_url}/openmrs/ws/rest/v1/appointment/{hms_booking_id}/changeStatus",
            headers=headers,
            json={"toStatus": "Cancelled", "onDate": datetime.now(timezone.utc).isoformat()},
        )
        if resp.status_code == 404:
            return CancelResult(status="NOT_FOUND")
        if resp.status_code >= 500:
            return CancelResult(status="FAILED", error_detail=f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return CancelResult(status="SUCCESS")

    async def push_visit_event(
        self,
        event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> None:
        headers = await self._get_headers()
        if isinstance(event, VisitCheckedIn):
            payload = {"status": "CheckedIn", "idempotencyKey": str(event.event_uuid)}
        elif isinstance(event, VisitConsultationStarted):
            payload = {"status": "InConsultation", "idempotencyKey": str(event.event_uuid)}
        else:
            status_map = {
                "completed": "Completed",
                "no_show": "Missed",
                "cancelled_after_arrival": "Cancelled",
            }
            payload = {
                "status": status_map.get(event.final_status, "Completed"),
                "idempotencyKey": str(event.event_uuid),
            }
        resp = await self._client.post(
            f"{self._base_url}/openmrs/ws/rest/v1/appointment/{event.appointment_id}/changeStatus",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 500:
            raise TransientError(f"Bahmni error: {resp.status_code}")
        resp.raise_for_status()

    async def health_check(self) -> AdapterHealth:
        start = time.monotonic()
        try:
            headers = await self._get_headers()
            resp = await self._client.get(
                f"{self._base_url}/openmrs/ws/rest/v1/session",
                headers=headers,
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
