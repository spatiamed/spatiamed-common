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
    AppointmentWrite,
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
from sm_common.integrations.exceptions import (
    ConflictError,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.hms_adapter import HmsAdapter
from sm_common.phone import hash_phone_for_lookup, phone_search_variants

logger = logging.getLogger(__name__)

# Our booking id travels on every Appointment we create, so a retry can find
# the one it already wrote. Changing this string orphans every earlier write.
BOOKING_IDENTIFIER_SYSTEM = "https://spatiamed.com/booking"


def _refusal(resp: httpx.Response) -> str:
    """A vendor refusal, PHI-safe: status code plus OperationOutcome issue codes.

    Never the body text — an OperationOutcome can echo the submitted resource
    (patient reference, reason), and these messages become job.last_error,
    booking error detail and log lines.
    """
    codes: list[str] = []
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for issue in body.get("issue") or []:
            if isinstance(issue, dict) and isinstance(issue.get("code"), str):
                codes.append(issue["code"])
    return f"HTTP {resp.status_code} issue_codes={codes}"


def _ref_id(participant_actor_ref: str) -> str:
    return participant_actor_ref.split("/")[-1] if participant_actor_ref else ""


def _practitioner_display_name(resource: dict) -> str | None:  # type: ignore[type-arg]
    """First official/usual name, else the first that is not `old`.

    Its `text`, or prefix + given + family joined with spaces. None when there
    is no usable name — an admin then maps this practitioner by hand.
    """
    names = [n for n in resource.get("name") or [] if isinstance(n, dict)]
    chosen = next((n for n in names if n.get("use") in ("official", "usual")), None)
    if chosen is None:
        chosen = next((n for n in names if n.get("use") != "old"), None)
    if chosen is None:
        return None
    text = str(chosen.get("text") or "").strip()
    if text:
        return text
    parts = [
        *(chosen.get("prefix") or []),
        *(chosen.get("given") or []),
        chosen.get("family") or "",
    ]
    joined = " ".join(str(p).strip() for p in parts if p and str(p).strip())
    return joined or None


def _practitioner_identifiers(resource: dict) -> list[tuple[str, str]]:  # type: ignore[type-arg]
    """Every identifier with a non-empty value, as (system or "", value)."""
    out: list[tuple[str, str]] = []
    for ident in resource.get("identifier") or []:
        if not isinstance(ident, dict):
            continue
        value = str(ident.get("value") or "").strip()
        if value:
            out.append((str(ident.get("system") or ""), value))
    return out


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

    def _appointment_instant(self, value: str) -> datetime:
        """Parse an Appointment.start/end as the instant it names.

        Standard FHIR: the value carries its own offset. Vendors whose offset
        label cannot be trusted override this (see OpenEmrAdapter).
        """
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _to_canonical(self, appt: dict) -> CanonicalAppointment:  # type: ignore[type-arg]
        meta = appt.get("meta", {})
        start = appt.get("start", "")
        # No fallback. Substituting datetime.now(UTC) placed the patient in
        # today's queue at the current moment rather than their real slot, and
        # nothing downstream could tell the time had been invented. A hosted
        # FHIR server returned two such values in 2111 appointments. Callers
        # skip the appointment instead — see list_appointments_modified_since.
        slot_start = self._appointment_instant(start)
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

        # _lastUpdated carries only the cursor's lower bound. until_date is a
        # clinic-day horizon on the appointment itself, so it belongs on `date` —
        # as an upper bound on the MODIFICATION time it silently excluded
        # everything changed today, which is precisely what the poll exists to see.
        # No _count. A server that honours it without emitting next links (and
        # without honouring _offset) turns the cap into silent truncation with no
        # way to ask for the rest. Let the server pick its own page size and
        # advertise the remainder through link[relation=next], as FHIR requires.
        resp = await self._client.get(
            f"{self._base}/Appointment",
            params={
                "_lastUpdated": f"gt{since}",
                "date": f"le{until_date.isoformat()}",
                "_sort": "_lastUpdated",
            },
            headers=headers,
        )
        resp.raise_for_status()

        appts: list[CanonicalAppointment] = []
        new_cursor = cursor
        reported_total: int | None = None
        # Appointment resources the SERVER actually handed us, whether or not we
        # could use them. The truncation guard below must measure delivery, not
        # parseability — otherwise skipping one unreadable appointment looks
        # identical to the server withholding records.
        appointments_received = 0

        while True:
            bundle = resp.json()
            if reported_total is None and isinstance(bundle.get("total"), int):
                reported_total = bundle["total"]
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                if res.get("resourceType") != "Appointment":
                    continue
                appointments_received += 1
                try:
                    appts.append(self._to_canonical(res))
                except (ValueError, TypeError, AttributeError):
                    logger.warning(
                        "FhirR4Adapter: skipping appointment %s — unusable start %r",
                        res.get("id", "<unknown>"),
                        res.get("start"),
                    )
                    continue
                lu = res.get("meta", {}).get("lastUpdated", "")
                if lu > new_cursor:
                    new_cursor = lu

            next_url = self._next_url(bundle)
            if not next_url:
                break

            # Fetch the next page using the server-supplied URL (auth headers re-attached)
            resp = await self._client.get(next_url, headers=headers)
            resp.raise_for_status()

        # The server said how many matched. Coming up short with nowhere left to
        # page means it withheld records, and advancing the cursor would skip
        # them permanently — not every server honours _sort, so they would not
        # simply arrive late.
        if reported_total is not None and appointments_received < reported_total:
            raise TransientError(
                f"Vendor withheld records: bundle reported total={reported_total} "
                f"but returned {appointments_received} with no next link. "
                "Cannot advance the cursor without losing the remainder."
            )

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

        # telecom carries the raw number. It has to be hashed into the shared
        # lookup space before it leaves this method — the field is named
        # phone_hash and every consumer compares it against stored hashes.
        phone_hash = ""
        for telecom in resource.get("telecom", []):
            if telecom.get("system") == "phone":
                raw_phone = telecom.get("value", "")
                phone_hash = hash_phone_for_lookup(raw_phone, self._hash_salt) if raw_phone else ""
                break

        return CanonicalPatient(
            mrn=mrn,
            abha_id=abha_id,
            phone_hash=phone_hash,
            name_token=name_token,
            age=self._age_from_birthdate(resource.get("birthDate")),
            gender=self._gender_code(resource.get("gender")),  # type: ignore[arg-type]
            resource_id=str(resource.get("id")) if resource.get("id") else None,
        )

    def _patient_query(
        self, phone_hash: str | None, mrn: str | None, abha_id: str | None, phone: str | None
    ) -> dict | None:  # type: ignore[type-arg]
        # Preference order: MRN, then ABHA, then phone. A phone is the weakest
        # hint — one number commonly serves a whole household in India — so
        # callers MUST corroborate the result against name, age and gender.
        if mrn:
            return {"identifier": mrn}
        if abha_id:
            return {"identifier": abha_id}
        if phone:
            # `telecom` is a TOKEN search parameter: it matches EXACTLY. Comma is
            # OR in FHIR search, so ask for every shape the number plausibly takes.
            variants = phone_search_variants(phone)
            return {"telecom": ",".join(variants)} if variants else None
        # A hash matches nothing on any vendor system; asking would disclose
        # that we are looking for someone.
        return None

    async def search_patients(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
    ) -> list[CanonicalPatient]:
        params = self._patient_query(phone_hash, mrn, abha_id, phone)
        if params is None:
            return []
        try:
            headers = await self._headers()
            resp = await self._client.get(f"{self._base}/Patient", params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.search_patients HTTP error: %s", exc)
            return []
        return [
            self._patient_to_canonical(e.get("resource", {}))
            for e in resp.json().get("entry", [])
            if e.get("resource", {}).get("resourceType", "Patient") == "Patient"
        ]

    async def find_patient(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
    ) -> CanonicalPatient | None:
        # Exactly one or nothing. Returning entries[0] of several silently
        # picked one member of a household; callers that must see ambiguity
        # use search_patients.
        found = await self.search_patients(
            phone_hash=phone_hash, mrn=mrn, abha_id=abha_id, phone=phone
        )
        return found[0] if len(found) == 1 else None

    async def get_patient(self, external_id: str) -> CanonicalPatient | None:
        if not external_id:
            return None
        try:
            resp = await self._client.get(
                f"{self._base}/Patient/{external_id}",
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            logger.warning("FhirR4Adapter.get_patient HTTP error for %s: %s", external_id, exc)
            return None

        if resp.status_code != 200:
            return None

        resource = resp.json()
        if resource.get("resourceType") != "Patient":
            return None
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

        active = resource.get("active")

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
            display_name=_practitioner_display_name(resource),
            identifiers=_practitioner_identifiers(resource),
            active=active if isinstance(active, bool) else None,
        )

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        """Fetch all active Practitioners from the FHIR server.

        Note: ``as_of_date`` is accepted for interface compatibility but is NOT
        forwarded to the server — FHIR R4 Practitioner has no standard
        ``_lastUpdated`` date filter that is reliably implemented across vendors.
        A full roster is always returned.
        """
        logger.debug(
            "FhirR4Adapter.fetch_doctor_roster: as_of_date=%s is not applied "
            "(FHIR R4 Practitioner has no standard date filter); returning full roster.",
            as_of_date,
        )
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
        # Same reasoning as _to_canonical: an invented time here would show up
        # as reconciliation drift against a slot that was never real.
        slot_start = self._appointment_instant(start_str)

        updated_str = resource.get("meta", {}).get("lastUpdated", "")
        try:
            updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            logger.warning(
                "FhirR4Adapter._booking_to_external: could not parse meta.lastUpdated %r "
                "for appointment %s; falling back to datetime.now(UTC).",
                updated_str,
                resource.get("id", "<unknown>"),
            )
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
                try:
                    bookings.append(self._booking_to_external(resource))
                except (ValueError, TypeError, AttributeError):
                    logger.warning(
                        "FhirR4Adapter: skipping reconciliation booking %s — unusable start %r",
                        resource.get("id", "<unknown>"),
                        resource.get("start"),
                    )
                    continue

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

    def _appointment_resource(self, write: AppointmentWrite) -> dict:  # type: ignore[type-arg]
        participants = [
            {"actor": {"reference": f"Patient/{write.patient_ref}"}, "status": "accepted"}
        ]
        if write.practitioner_ref:
            participants.append(
                {
                    "actor": {"reference": f"Practitioner/{write.practitioner_ref}"},
                    "status": "accepted",
                }
            )
        resource: dict = {  # type: ignore[type-arg]
            "resourceType": "Appointment",
            "status": "booked",
            "start": write.start.isoformat(),
            "end": write.end.isoformat(),
            "participant": participants,
            "identifier": [{"system": BOOKING_IDENTIFIER_SYSTEM, "value": str(write.booking_id)}],
        }
        if write.reason:
            resource["description"] = write.reason
        return resource

    def _result_from_resource(self, resource: dict, *, created: bool) -> WriteBackResult:  # type: ignore[type-arg]
        appt_id = resource.get("id")
        if resource.get("resourceType") != "Appointment" or not appt_id:
            # FINDINGS §7.2: a 2xx is not evidence anything was created.
            # Keys only: a 2xx body can be the resource we sent, carrying PHI.
            raise VendorRejected(
                f"vendor response carries no Appointment id: resourceType="
                f"{resource.get('resourceType')!r} keys={sorted(resource)}"
            )
        start = resource.get("start")
        hms_start = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
        return WriteBackResult(
            status="SUCCESS", hms_booking_id=str(appt_id), hms_start=hms_start, created=created
        )

    async def _find_our_appointment(self, token: str, headers: dict[str, str]) -> dict | None:  # type: ignore[type-arg]
        """The appointment we already wrote for this booking, if the server can say.

        A server that rejects `identifier` search (4xx) cannot answer; the
        If-None-Exist header on the create is then our only guard.
        """
        try:
            resp = await self._client.get(
                f"{self._base}/Appointment", params={"identifier": token}, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"write_back pre-search HTTP error: {exc}") from exc
        if resp.status_code >= 500:
            raise TransientError(f"write_back pre-search HTTP {resp.status_code}")
        if resp.status_code >= 400:
            logger.warning(
                "FhirR4Adapter: server refused identifier search (HTTP %s)", resp.status_code
            )
            return None
        hits = [
            e["resource"]
            for e in resp.json().get("entry", [])
            if e.get("resource", {}).get("resourceType") == "Appointment"
        ]
        if len(hits) > 1:
            # Only we write this identifier: the HMS already holds it (twice).
            raise ConflictError(
                f"{len(hits)} appointments already carry {token} — a human must reconcile",
                landed=True,
            )
        return hits[0] if hits else None

    async def write_back_idempotent(self, write: AppointmentWrite) -> WriteBackResult:
        headers = await self._headers()
        token = f"{BOOKING_IDENTIFIER_SYSTEM}|{write.booking_id}"
        existing = await self._find_our_appointment(token, headers)
        if existing is not None:
            return self._result_from_resource(existing, created=False)

        try:
            resp = await self._client.post(
                f"{self._base}/Appointment",
                json=self._appointment_resource(write),
                headers={**headers, "If-None-Exist": f"identifier={token}"},
            )
        except httpx.HTTPError as exc:
            # Raised, not returned: WriteBackRouter only falls through on a raise.
            raise TransientError(f"write_back_idempotent HTTP error: {exc}") from exc

        if resp.status_code in (404, 405):
            raise WriteNotSupported(
                f"vendor has no Appointment create route (HTTP {resp.status_code})"
            )
        if resp.status_code == 409:
            raise ConflictError(f"write_back_idempotent {_refusal(resp)}")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"write_back_idempotent {_refusal(resp)}")
        if resp.status_code >= 400:
            raise VendorRejected(f"write_back_idempotent {_refusal(resp)}")
        if resp.status_code == 200 and not resp.content:
            # Conditional create matched an existing resource and the server
            # returned no body — look it up rather than guess.
            existing = await self._find_our_appointment(token, headers)
            if existing is None:
                raise TransientError(
                    "conditional create returned 200 with no body and no match found"
                )
            return self._result_from_resource(existing, created=False)
        return self._result_from_resource(resp.json(), created=resp.status_code == 201)

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
            # A compensation step that reports failure and moves on leaves a
            # live booking here and nothing in the HMS. Raise so it retries.
            raise TransientError(f"cancel HTTP error: {exc}") from exc

        if resp.status_code == 200:
            return CancelResult(status="SUCCESS")
        if resp.status_code == 404:
            # Nothing to cancel is an answer, not a failure — do not retry.
            return CancelResult(status="NOT_FOUND", error_detail=resp.text)

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"cancel {_refusal(resp)}")

        # A permanent 4xx needs a human, not another attempt (spec §1: cancel
        # raises on failure; only NOT_FOUND is a returned outcome).
        raise VendorRejected(f"cancel {_refusal(resp)}")

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
        except httpx.HTTPError as exc:
            raise TransientError(
                f"push_visit_event HTTP error for event {event.event_uuid}: {exc}"
            ) from exc

        if resp.status_code >= 300:
            # Silently dropping this made an event that never reached the HMS
            # indistinguishable from one that did.
            raise TransientError(
                f"push_visit_event HTTP {resp.status_code} for event {event.event_uuid}: "
                f"{resp.text[:200]}"
            )
