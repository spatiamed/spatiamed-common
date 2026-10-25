from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from sm_common.identity.dob import age_on
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp, from_epoch_ms, to_epoch_ms
from sm_common.integrations.adapters.bahmni_parse import birth_date as _birth_date
from sm_common.integrations.adapters.bahmni_parse import flag as _flag
from sm_common.integrations.adapters.bahmni_parse import gender as _gender
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.record_types import RecordAddress, RecordPatientInput
from sm_common.integrations.canonical_types import (
    AdapterHealth,
    AppointmentWrite,
    CancelResult,
    CanonicalAppointment,
    CanonicalDoctor,
    CanonicalPatient,
    ExternalBooking,
    PatientCreate,
    PatientCreateResult,
    VisitCheckedIn,
    VisitConsultationStarted,
    VisitFinalized,
    WriteBackResult,
)
from sm_common.integrations.exceptions import (
    AuthError,
    ConflictError,
    NotFound,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.hms_adapter import HmsAdapter
from sm_common.phone import hash_phone_for_lookup, phone_search_variants

BahmniStatus = Literal["Scheduled", "CheckedIn", "Completed", "Cancelled", "Missed"]
_FINAL_STATUS: dict[str, BahmniStatus] = {
    "completed": "Completed",
    "no_show": "Missed",
    "cancelled_after_arrival": "Cancelled",
}
_REST = "/openmrs/ws/rest/v1"
_SEARCH_CAP = 50
_SEARCH_CALL_BUDGET = 64  # POSTs one top-level _search_window may spend
_FEED = "/openmrs/ws/atomfeed/appointments"
_FEED_PAGE_CAP = 20  # prev-archive pages followed beyond "recent" while hunting the cursor
_ATOM = "{http://www.w3.org/2005/Atom}"
_MARKER = re.compile(r"^sm:\S+\s*")
log = logging.getLogger(__name__)
_TERMINAL_STATUSES = frozenset({"Cancelled", "Missed"})
_SEARCH_PAGE_CAP = 10  # pages of bahmni/search/patient followed per query
_SEARCH_PAGE_ROWS = 50  # rows Bahmni returns per bahmni/search/patient page
_ROSTER_PAGE = 100
_ROSTER_PAGE_CAP = 50


class BahmniAdapter(HmsAdapter):
    """HMS adapter for Bahmni/OpenMRS deployments.

    Every call goes through ``BahmniHttp`` (basic auth + session cookie, one
    re-login on 401, fixed error mapping). Per-integration uuids and concept
    names come from ``BahmniConfig``.
    """

    vendor_name = "bahmni"

    def __init__(
        self,
        http: BahmniHttp,
        config: BahmniConfig,
        hash_salt: str,
        transport_key: str,
    ) -> None:
        self._http = http
        self._config = config
        self._hash_salt = hash_salt
        self._transport_key = transport_key
        self._record = BahmniRecordClient(http, config)

    @staticmethod
    def _require_found(resp: Any, what: str) -> None:
        if resp.status_code == 404:
            raise VendorRejected(f"bahmni: {what} not found (HTTP 404)")

    async def _feed_page(self, path: str) -> tuple[list[tuple[str, str]], str | None]:
        """One AtomFeed page -> ([(entry id, content path)] oldest first, prev-archive path)."""
        resp = await self._http.request("GET", path, expect=(200,))
        self._require_found(resp, "appointment feed")
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            raise TransientError(f"bahmni: unreadable appointment feed ({exc})") from exc
        entries: list[tuple[str, str, str]] = []
        for e in root.findall(f"{_ATOM}entry"):
            eid = (e.findtext(f"{_ATOM}id") or "").strip()
            content = (e.findtext(f"{_ATOM}content") or "").strip()
            if eid and content:
                entries.append((eid, content, (e.findtext(f"{_ATOM}updated") or "").strip()))
        # Atom does not fix the order inside a page: read it off the timestamps. Only a
        # strictly newer first entry means "newest first"; a tie (e.g. a book and a cancel
        # in the same second) keeps document order. Live page order: verified in Task 9.
        if len(entries) > 1 and entries[0][2] > entries[-1][2]:
            entries.reverse()
        prev: str | None = None
        for link in root.findall(f"{_ATOM}link"):
            if link.get("rel") == "prev-archive" and link.get("href"):
                parts = urlsplit(link.get("href", ""))
                prev = parts.path + (f"?{parts.query}" if parts.query else "")
        return [(eid, content) for eid, content, _ in entries], prev

    async def list_appointments_modified_since(
        self, cursor: str, until_date: date
    ) -> tuple[list[CanonicalAppointment], str]:
        """Appointments changed since ``cursor`` (an atom entry id), oldest change first.

        An empty or non-atom cursor (e.g. a legacy ISO timestamp) means first run:
        only the ``recent`` page is read. Otherwise ``prev-archive`` is followed
        (at most ``_FEED_PAGE_CAP`` pages) until the cursor is found. A cursor that
        is never found is logged as ``cursor_lost`` and everything walked is returned.
        """
        tracking = bool(cursor) and not _is_timestamp(cursor)
        pages: list[list[tuple[str, str]]] = []  # newest page first
        path: str | None = f"{_FEED}/recent"
        found = False
        walked = 0
        while path is not None:
            entries, prev = await self._feed_page(path)
            pages.append(entries)
            if tracking and any(eid == cursor for eid, _ in entries):
                found = True
                break
            if not tracking or walked >= _FEED_PAGE_CAP or prev in (None, path):
                break
            path = prev
            walked += 1
        chronological = [e for page in reversed(pages) for e in page]
        if tracking and found:
            at = max(i for i, (eid, _) in enumerate(chronological) if eid == cursor)
            chronological = chronological[at + 1 :]
        elif tracking:
            log.warning(
                "cursor_lost: %r not in %d feed page(s); processing what was walked",
                cursor,
                len(pages),
            )
        if not chronological:
            return [], cursor
        new_cursor = chronological[-1][0]
        # Dedupe by appointment, keeping the last event (a cancel repeats the uuid).
        order: dict[str, None] = {}
        for _, content in chronological:
            uuid = content.rstrip("/").rsplit("/", 1)[-1]
            order.pop(uuid, None)
            order[uuid] = None
        appointments: list[CanonicalAppointment] = []
        for uuid in order:
            resp = await self._http.request("GET", f"{_REST}/appointments/{uuid}")
            if resp.status_code == 404:
                log.warning("feed_entry_gone: appointment %s", uuid)
                continue
            body = resp.json()
            if isinstance(body, dict):
                appointments.append(self._appointment(body))
            else:
                log.warning("feed_entry_unreadable: appointment %s body is not an object", uuid)
        return appointments, new_cursor

    def _appointment(self, a: Mapping[str, Any]) -> CanonicalAppointment:
        start = int(a["startDateTime"])
        end = int(a["endDateTime"])
        p = a.get("patient") or {}
        service = a.get("service") or {}
        providers = a.get("providers") or []
        comments = a.get("comments")
        reason = _MARKER.sub("", comments).strip() if isinstance(comments, str) else ""
        age = p.get("age")
        return CanonicalAppointment(
            appointment_id=str(a["uuid"]),
            hms_version=int(a.get("dateChanged") or a.get("dateCreated") or 0),
            patient=CanonicalPatient(
                mrn=str(p.get("identifier") or ""),
                abha_id=None,
                phone_hash="",
                name_token="",
                age=age if isinstance(age, int) and not isinstance(age, bool) else None,
                gender=_gender(p.get("gender")),
                resource_id=str(p["uuid"]) if p.get("uuid") else None,
            ),
            doctor_external_id=str(providers[0].get("uuid") or "") if providers else "",
            department_external_id=str(service.get("uuid") or ""),
            slot_start=from_epoch_ms(start),
            slot_duration_min=(end - start) // 60000,
            payer_type="CASH",
            reason_text=reason or None,
            status=str(a.get("status") or "Scheduled"),
        )

    async def _search_window(
        self, start: datetime, end: datetime
    ) -> tuple[list[dict[str, Any]], bool]:
        """Appointments in [start, end] -> (rows, truncated).

        ``appointments/search`` returns at most 50 rows and cannot paginate, so a
        full answer is split in half until each part is under the cap. ``truncated``
        is True when a window of one minute or less is still full, or when the call
        budget (``_SEARCH_CALL_BUDGET`` POSTs) runs out, e.g. if the server ignores
        the date window. Bounds are sent in whole seconds.
        """
        calls = [0]
        return await self._bisect(start.replace(microsecond=0), end.replace(microsecond=0), calls)

    async def _bisect(
        self, start: datetime, end: datetime, calls: list[int]
    ) -> tuple[list[dict[str, Any]], bool]:
        if calls[0] >= _SEARCH_CALL_BUDGET:
            return [], True
        calls[0] += 1
        resp = await self._http.request(
            "POST",
            f"{_REST}/appointments/search",
            json={
                "startDate": start.astimezone(timezone.utc).isoformat(),
                "endDate": end.astimezone(timezone.utc).isoformat(),
            },
        )
        # Fail closed: a lookup we cannot trust must never fall through to a POST.
        if resp.status_code != 200:
            raise WriteNotSupported(
                f"bahmni: appointment search unavailable (HTTP {resp.status_code})"
            )
        rows = resp.json()
        if not isinstance(rows, list):
            raise TransientError("bahmni: appointment search returned a non-list body")
        if len(rows) < _SEARCH_CAP:
            return rows, False
        if end - start <= timedelta(minutes=1):
            return rows, True
        mid = (start + (end - start) / 2).replace(microsecond=0)
        left, left_cut = await self._bisect(start, mid, calls)
        right, right_cut = await self._bisect(mid, end, calls)
        merged: dict[str, dict[str, Any]] = {}
        for row in [*left, *right]:
            merged.setdefault(str(row.get("uuid")), row)
        return list(merged.values()), left_cut or right_cut

    # ─── Patient read ────────────────────────────────────────────────────────

    def _phone_hash(self, raw: Any) -> str:
        return hash_phone_for_lookup(raw, self._hash_salt) if isinstance(raw, str) and raw else ""

    def _canonical(
        self,
        *,
        uuid: str,
        mrn: str,
        given: str,
        family: str,
        gender: Any,
        birth: Any,
        estimated: bool,
        phone: Any,
    ) -> CanonicalPatient:
        dob = _birth_date(birth)
        return CanonicalPatient(
            mrn=mrn,
            abha_id=None,
            phone_hash=self._phone_hash(phone),
            name_token=" ".join(x for x in (given.strip(), family.strip()) if x),
            age=age_on(dob, datetime.now(timezone.utc).date()) if dob else None,
            gender=_gender(gender),
            resource_id=uuid,
            birth_date=dob,
            birth_date_precision=("year" if estimated else "exact") if dob else None,
        )

    def _from_search_hit(self, hit: Mapping[str, Any]) -> CanonicalPatient | None:
        uuid = hit.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            return None
        attrs: Any = hit.get("customAttribute")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = None
        phone = attrs.get(self._config.phone_attribute_name) if isinstance(attrs, dict) else None
        return self._canonical(
            uuid=uuid,
            mrn=str(hit.get("identifier") or ""),
            given=str(hit.get("givenName") or ""),
            family=str(hit.get("familyName") or ""),
            gender=hit.get("gender"),
            birth=hit.get("birthDate"),
            estimated=_flag(hit.get("birthdateEstimated")),
            phone=phone,
        )

    async def _search(self, **query: str) -> list[CanonicalPatient]:
        params = {
            "q": "",
            "patientAttributes": self._config.phone_attribute_name,
            # Without this Bahmni returns customAttribute "" and the phone is lost.
            "patientSearchResultsConfig": self._config.phone_attribute_name,
            "s": "byIdOrNameOrVillage",
            **query,
        }
        found: list[CanonicalPatient] = []
        seen = 0
        for _ in range(_SEARCH_PAGE_CAP):
            resp = await self._http.request(
                "GET",
                f"{_REST}/bahmni/search/patient",
                params={**params, "startIndex": str(seen)},
            )
            if resp.status_code == 404:
                break
            body = resp.json()
            page = body.get("pageOfResults") if isinstance(body, dict) else None
            if not page:
                break
            seen += len(page)
            for hit in page:
                p = self._from_search_hit(hit) if isinstance(hit, dict) else None
                if p is not None:
                    found.append(p)
            # Bahmni caps totalCount at the page it returned (50 rows), so a full page
            # means "maybe more": only a short page, with totalCount reached, ends the walk.
            total = body.get("totalCount")
            if len(page) < _SEARCH_PAGE_ROWS and (not isinstance(total, int) or seen >= total):
                break
        return found

    async def search_patients(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
        family: str | None = None,
        birth_date: date | None = None,
    ) -> list[CanonicalPatient]:
        """Every Bahmni hit. ``phone_hash`` alone matches nothing on Bahmni: ``[]``, no HTTP."""
        hits: list[CanonicalPatient]
        if family is not None or birth_date is not None:
            # A blank family would otherwise answer [] ("nobody"), which unlocks Create.
            if not (family or "").strip() or birth_date is None:
                raise ValueError("family and birth_date are searched together")
            if phone or mrn or abha_id:
                raise ValueError("one patient search per call")
            hits = await self._search(q=family.strip())
            return _dedupe([p for p in hits if p.birth_date == birth_date])
        if mrn:
            return _dedupe(await self._search(identifier=mrn))
        if phone:
            hits = []
            for variant in phone_search_variants(phone):
                hits += await self._search(customAttribute=variant)
            return _dedupe(hits)
        return []

    async def find_patient(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
    ) -> CanonicalPatient | None:
        hits = await self.search_patients(phone_hash=phone_hash, mrn=mrn, phone=phone)
        return hits[0] if len(hits) == 1 else None

    async def get_patient(self, external_id: str) -> CanonicalPatient | None:
        resp = await self._http.request(
            "GET", f"{_REST}/patientprofile/{external_id}", params={"v": "full"}
        )
        if resp.status_code == 404:
            return None
        body = resp.json()
        patient = body.get("patient") if isinstance(body, dict) else None
        if not isinstance(patient, dict):
            return None
        person = patient.get("person") or {}
        idents = [i for i in patient.get("identifiers") or [] if isinstance(i, dict)]
        preferred = next((i for i in idents if i.get("preferred")), idents[0] if idents else {})
        name = next((n for n in person.get("names") or [] if isinstance(n, dict)), {})
        phone = next(
            (
                a.get("value")
                for a in person.get("attributes") or []
                if isinstance(a, dict)
                and (a.get("attributeType") or {}).get("display")
                == self._config.phone_attribute_name
            ),
            None,
        )
        return self._canonical(
            uuid=str(patient.get("uuid") or external_id),
            mrn=str(preferred.get("identifier") or ""),
            given=str(name.get("givenName") or ""),
            family=str(name.get("familyName") or ""),
            gender=person.get("gender"),
            birth=person.get("birthdate"),
            estimated=_flag(person.get("birthdateEstimated")),
            phone=phone if isinstance(phone, str) else None,
        )

    async def create_patient(self, patient: PatientCreate) -> PatientCreateResult:
        """Register a NEW patient via the record client (idempotent on the marker, if configured)."""
        rec, created = await self._record.create_patient(
            RecordPatientInput(
                marker=patient.patient_marker,
                given=" ".join(patient.given),
                family=patient.family,
                gender=patient.gender,
                birth_date=patient.birth_date,
                birth_date_estimated=False,
                phone=patient.phone,
                address=RecordAddress(),
                exclude_ids=patient.exclude_ids,
            )
        )
        return PatientCreateResult(
            resource_id=rec.uuid, mrn=rec.identifier or None, created=created
        )

    # ─── Roster ──────────────────────────────────────────────────────────────

    async def _service_names(self) -> dict[str, str]:
        if not self._config.service_by_practitioner:
            return {}
        resp = await self._http.request("GET", f"{_REST}/appointmentService/all/default")
        if resp.status_code == 404:
            return {}
        body = resp.json()
        rows = body.get("results", []) if isinstance(body, dict) else body
        return {
            r["uuid"]: str(r.get("name") or "")
            for r in rows or []
            if isinstance(r, dict) and r.get("uuid")
        }

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        providers: list[dict[str, Any]] = []
        for page in range(_ROSTER_PAGE_CAP):
            resp = await self._http.request(
                "GET",
                f"{_REST}/provider",
                params={
                    "v": "full",
                    "limit": str(_ROSTER_PAGE),
                    "startIndex": str(page * _ROSTER_PAGE),
                },
            )
            body = resp.json() if resp.status_code != 404 else {}
            providers += [r for r in body.get("results", []) if isinstance(r, dict)]
            has_next = any(
                isinstance(link, dict) and link.get("rel") == "next"
                for link in body.get("links") or []
            )
            if not has_next:
                break
        service_names = await self._service_names()
        doctors = []
        for p in providers:
            person = p.get("person")
            identifier = str(p.get("identifier") or "")
            if (
                p.get("retired")
                or not isinstance(person, dict)
                or identifier in self._config.system_provider_identifiers
            ):
                continue
            uuid = str(p.get("uuid") or "")
            service_uuid = self._config.service_by_practitioner.get(uuid, "")
            doctors.append(
                CanonicalDoctor(
                    external_doctor_id=uuid,
                    external_speciality_id=service_uuid,
                    external_department_id="",
                    external_sub_dept_id=None,
                    speciality_label=service_names.get(service_uuid, ""),
                    department_label="",
                    consultation_fee_inr=None,
                    consultation_duration_min=15,
                    display_name=person.get("display") or None,
                    identifiers=[("", identifier)] if identifier else [],
                    active=not p.get("retired"),
                )
            )
        return doctors

    async def fetch_recent_bookings(
        self, hospital_id: UUID, lookback_minutes: int
    ) -> list[ExternalBooking]:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rows, truncated = await self._search_window(
            now - timedelta(minutes=lookback_minutes), now + timedelta(days=1)
        )
        if truncated:
            log.warning("search_truncated: a one-minute window still returned %d rows", _SEARCH_CAP)
        bookings = []
        for b in rows:
            changed = b.get("dateChanged") or b.get("dateCreated")
            try:
                bookings.append(
                    ExternalBooking(
                        appointment_id=str(b["uuid"]),
                        doctor_external_id=str(((b.get("providers") or [{}])[0]).get("uuid") or ""),
                        slot_start=from_epoch_ms(b["startDateTime"]),
                        status=str(b.get("status") or ""),
                        updated_at=from_epoch_ms(changed),
                    )
                )
            except (KeyError, ValueError, TypeError):
                log.warning("search row skipped: %r", b.get("uuid"))
        return bookings

    async def write_back_idempotent(self, write: AppointmentWrite) -> WriteBackResult:
        """Create the appointment unless a live one carrying our marker exists.

        Bahmni neither dedupes nor reports slot conflicts, so idempotency is
        client-side: appointments written here start their ``comments`` with
        ``sm:<booking_id>``. Cancelled and Missed rows are terminal and never
        match, so a retry of a cancelled booking creates a fresh appointment.
        """
        marker = f"sm:{write.booking_id}"
        # Resolve config first: an unmapped service must fail before any I/O.
        service_uuid = write.service_ref or self._config.service_for(write.practitioner_ref)
        rows, truncated = await self._search_window(
            write.start - timedelta(minutes=1), write.end + timedelta(minutes=1)
        )
        if truncated:
            raise TransientError(
                "bahmni: appointment search hit its row cap; the marker lookup is unreliable"
            )
        for row in rows:
            comments = row.get("comments")
            if (
                isinstance(comments, str)
                and (comments == marker or comments.startswith(marker + " "))
                and row.get("status") not in _TERMINAL_STATUSES
                and row.get("uuid")
            ):
                return WriteBackResult(
                    status="SUCCESS",
                    hms_booking_id=str(row["uuid"]),
                    hms_start=from_epoch_ms(row["startDateTime"]),
                    created=False,
                )
        body = {
            "patientUuid": write.patient_ref,
            "serviceUuid": service_uuid,
            "providers": (
                [{"uuid": write.practitioner_ref, "response": "ACCEPTED"}]
                if write.practitioner_ref
                else []
            ),
            "locationUuid": self._config.location_uuid,
            "startDateTime": to_epoch_ms(write.start),
            "endDateTime": to_epoch_ms(write.end),
            "appointmentKind": "Scheduled",
            "comments": marker + (f" {write.reason}" if write.reason else ""),
        }
        # A 400 propagates as VendorRejected (never retried); 5xx as TransientError.
        created = await self._http.request("POST", f"{_REST}/appointment", json=body)
        if created.status_code == 404:
            raise WriteNotSupported("bahmni: appointment route not found (HTTP 404)")
        data = created.json()
        return WriteBackResult(
            status="SUCCESS",
            hms_booking_id=str(data["uuid"]),
            hms_start=from_epoch_ms(data["startDateTime"]),
            created=True,
        )

    async def _set_status(
        self, appointment_uuid: str, to_status: BahmniStatus, comments: str | None = None
    ) -> str:
        """Move an appointment to ``to_status``; idempotent on the current status.

        Bahmni answers 500 to a repeat change, so the current status is read first.
        Raises ``NotFound`` on 404, ``TransientError`` on 5xx.
        """
        current = await self._http.request("GET", f"{_REST}/appointments/{appointment_uuid}")
        if current.status_code == 404:
            raise NotFound(f"bahmni: appointment {appointment_uuid} not found (HTTP 404)")
        data = current.json()
        status = str(data.get("status")) if isinstance(data, dict) else ""
        if status == to_status:
            return status
        body: dict[str, Any] = {
            "toStatus": to_status,
            "onDate": None,
            "timeZone": "Asia/Kolkata",
        }
        if comments:
            body["comments"] = comments
        resp = await self._http.request(
            "POST", f"{_REST}/appointments/{appointment_uuid}/status-change", json=body
        )
        if resp.status_code == 404:
            raise NotFound(f"bahmni: appointment {appointment_uuid} not found (HTTP 404)")
        return to_status

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        try:
            await self._set_status(hms_booking_id, "Cancelled", reason)
        except NotFound:
            return CancelResult(status="NOT_FOUND")
        except TransientError as exc:
            return CancelResult(status="FAILED", error_detail=str(exc))
        return CancelResult(status="SUCCESS")

    async def push_visit_event(
        self,
        event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> None:
        if isinstance(event, VisitConsultationStarted):
            return  # Bahmni has no in-consultation status
        to_status: BahmniStatus
        if isinstance(event, VisitCheckedIn):
            to_status = "CheckedIn"
        else:
            to_status = _FINAL_STATUS[event.final_status]
        await self._set_status(event.appointment_id, to_status)

    async def health_check(self) -> AdapterHealth:
        start = time.monotonic()
        try:
            await self._http.request("GET", f"{_REST}/session", expect=(200,))
        except TransientError as exc:
            return AdapterHealth(
                healthy=False,
                last_success_at=None,
                latency_ms=None,
                message=f"connection error: {exc}",
            )
        except (AuthError, VendorRejected) as exc:
            return AdapterHealth(
                healthy=False,
                last_success_at=None,
                latency_ms=int((time.monotonic() - start) * 1000),
                message=f"auth error: {exc}",
            )
        return AdapterHealth(
            healthy=True,
            last_success_at=datetime.now(timezone.utc),
            latency_ms=int((time.monotonic() - start) * 1000),
            message="OK",
        )


def _is_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _dedupe(patients: list[CanonicalPatient]) -> list[CanonicalPatient]:
    seen: set[str | None] = set()
    out = []
    for p in patients:
        if p.resource_id in seen:
            continue
        seen.add(p.resource_id)
        out.append(p)
    return out
