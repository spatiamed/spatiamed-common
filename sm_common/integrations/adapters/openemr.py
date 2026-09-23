"""OpenEMR: reads through FHIR, appointment writes through the Standard API.

Measured against OpenEMR 8.3.0 (harness/openemr/FINDINGS.md):
- FHIR Appointment is read-only; the only create is
  POST /api/patient/:pid/appointment on the Standard API.
- The Standard API refuses system tokens, so writes carry a user-role
  password-grant token (auth scheme `oauth2_password`) for a dedicated,
  appointment-only service account the hospital issues.
- pc_eventDate / pc_startTime are server-local with no offset: every time is
  converted through the integration's configured IANA timezone.
- The create returns the numeric pc_eid, but ingest keys on the FHIR
  Appointment.id, which is pc_uuid. We always return pc_uuid.
- pc_hometext (our idempotency marker) is NOT in the per-patient list, only in
  the single-appointment GET — so the guard narrows by date/time/provider,
  then reads each candidate.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.auth import build_auth_headers, invalidate_token
from sm_common.integrations.canonical_types import (
    AppointmentWrite,
    CancelResult,
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

logger = logging.getLogger(__name__)


def _default_standard_base(fhir_base: str) -> str:
    base = fhir_base.rstrip("/")
    return base[: -len("/fhir")] + "/api" if base.endswith("/fhir") else base


class OpenEmrAdapter(FhirR4Adapter):
    vendor_name = "openemr"

    def __init__(
        self,
        *,
        base_url: str,
        auth_scheme: str,
        auth_cfg: dict,  # type: ignore[type-arg]
        openemr: dict,  # type: ignore[type-arg]
        hash_salt: str = "",
    ) -> None:
        super().__init__(
            base_url=base_url, auth_scheme=auth_scheme, auth_cfg=auth_cfg, hash_salt=hash_salt
        )
        for key in ("timezone", "pc_catid", "pc_facility", "pc_billing_location", "write_user"):
            if not openemr.get(key):
                raise ValueError(f"openemr integration config is missing {key!r}")
        self._std = (openemr.get("standard_base_url") or _default_standard_base(base_url)).rstrip(
            "/"
        )
        self._tz = ZoneInfo(openemr["timezone"])
        self._write_cfg: dict = dict(openemr["write_user"])  # type: ignore[type-arg]
        self._defaults = {
            "pc_catid": str(openemr["pc_catid"]),
            "pc_facility": str(openemr["pc_facility"]),
            "pc_billing_location": str(openemr["pc_billing_location"]),
            "pc_apptstatus": str(openemr.get("pc_apptstatus", "-")),
        }
        self._pid_cache: dict[str, str] = {}
        self._aid_cache: dict[str, str] = {}

    # ─── Standard API plumbing ────────────────────────────────────────────

    async def _std_request(
        self, method: str, path: str, json_body: dict | None = None
    ) -> httpx.Response:  # type: ignore[type-arg]
        for attempt in (1, 2):
            headers = await build_auth_headers(self._client, "oauth2_password", self._write_cfg)
            try:
                resp = await self._client.request(
                    method, f"{self._std}{path}", json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                raise TransientError(f"OpenEMR {method} {path}: {exc}") from exc
            if resp.status_code == 401 and attempt == 1:
                invalidate_token(self._write_cfg)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                raise TransientError(f"OpenEMR {method} {path}: HTTP {resp.status_code}")
            return resp
        return resp

    @staticmethod
    def _data(resp: httpx.Response) -> Any:
        body = resp.json()
        if isinstance(body, dict) and (body.get("validationErrors") or body.get("internalErrors")):
            raise VendorRejected(f"OpenEMR refused: {str(body)[:300]}")
        return body.get("data") if isinstance(body, dict) and "data" in body else body

    @staticmethod
    def _row(data: Any) -> dict:  # type: ignore[type-arg]
        return (
            data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
        )

    async def _pid(self, patient_ref: str) -> str:
        if patient_ref not in self._pid_cache:
            resp = await self._std_request("GET", f"/patient/{patient_ref}")
            if resp.status_code == 404:
                raise VendorRejected(f"OpenEMR has no patient {patient_ref}")
            pid = self._row(self._data(resp)).get("pid")
            if not pid:
                raise VendorRejected(f"OpenEMR patient {patient_ref} has no pid")
            self._pid_cache[patient_ref] = str(pid)
        return self._pid_cache[patient_ref]

    async def _aid(self, practitioner_ref: str) -> str:
        if practitioner_ref not in self._aid_cache:
            resp = await self._std_request("GET", f"/practitioner/{practitioner_ref}")
            if resp.status_code == 404:
                raise VendorRejected(f"OpenEMR has no practitioner {practitioner_ref}")
            aid = self._row(self._data(resp)).get("id")
            if not aid:
                raise VendorRejected(f"OpenEMR practitioner {practitioner_ref} has no user id")
            self._aid_cache[practitioner_ref] = str(aid)
        return self._aid_cache[practitioner_ref]

    async def _get_appt(self, eid: str) -> dict:  # type: ignore[type-arg]
        resp = await self._std_request("GET", f"/appointment/{eid}")
        row = self._row(self._data(resp))
        if not row:
            raise TransientError(f"OpenEMR appointment {eid} could not be read back")
        return row

    def _created_eid(self, resp: httpx.Response) -> str:
        data = self._data(resp)
        if isinstance(data, (int, str)) and str(data).isdigit():
            return str(data)
        row = self._row(data)
        eid = row.get("pc_eid") or row.get("id")
        if not eid:
            # FINDINGS §7.2: OpenEMR answers a failed create with HTTP 200 and a
            # validation map. A 200 is not evidence anything was created.
            raise VendorRejected(
                f"OpenEMR create returned no appointment id: {str(resp.text)[:300]}"
            )
        return str(eid)

    def _result(self, row: dict, *, created: bool) -> WriteBackResult:  # type: ignore[type-arg]
        uuid = row.get("pc_uuid")
        if not uuid:
            raise VendorRejected(f"OpenEMR appointment carries no pc_uuid: {str(row)[:200]}")
        local = datetime.combine(
            date.fromisoformat(str(row["pc_eventDate"])),
            time.fromisoformat(str(row["pc_startTime"])[:5]),
            tzinfo=self._tz,
        )
        return WriteBackResult(
            status="SUCCESS", hms_booking_id=str(uuid), hms_start=local, created=created
        )

    # ─── Writes ───────────────────────────────────────────────────────────

    async def _find_marked(
        self, pid: str, day: str, hhmm: str, aid: str | None, marker: str
    ) -> dict | None:  # type: ignore[type-arg]
        resp = await self._std_request("GET", f"/patient/{pid}/appointment")
        rows = self._data(resp) or []
        same_slot = [
            r
            for r in rows
            if str(r.get("pc_eventDate")) == day
            and str(r.get("pc_startTime", ""))[:5] == hhmm
            and (str(r.get("pc_aid") or "") == (aid or "") or (aid is None and not r.get("pc_aid")))
        ]
        for r in same_slot:
            full = await self._get_appt(str(r["pc_eid"]))
            if marker in str(full.get("pc_hometext") or ""):
                return full
        if same_slot:
            raise ConflictError(
                f"patient {pid} already has an appointment at {day} {hhmm} that we did not write"
            )
        return None

    async def write_back_idempotent(self, write: AppointmentWrite) -> WriteBackResult:
        pid = await self._pid(write.patient_ref)
        aid = await self._aid(write.practitioner_ref) if write.practitioner_ref else None
        local = write.start.astimezone(self._tz)
        day, hhmm = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
        marker = f"[spatiamed:{write.booking_id}]"

        existing = await self._find_marked(pid, day, hhmm, aid, marker)
        if existing is not None:
            return self._result(existing, created=False)

        body = {
            **self._defaults,
            "pc_title": (write.reason or "SpatiaMed booking")[:150],
            "pc_duration": str(int((write.end - write.start).total_seconds())),
            "pc_hometext": f"{write.reason or ''} {marker}".strip(),
            "pc_eventDate": day,
            "pc_startTime": hhmm,
        }
        if aid is not None:
            body["pc_aid"] = aid
        resp = await self._std_request("POST", f"/patient/{pid}/appointment", body)
        if resp.status_code in (404, 405):
            raise WriteNotSupported(
                f"OpenEMR has no appointment create route (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise VendorRejected(f"OpenEMR create HTTP {resp.status_code}: {resp.text[:200]}")
        eid = self._created_eid(resp)
        return self._result(await self._get_appt(eid), created=True)

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        # hms_booking_id is pc_uuid (the FHIR id). The delete route wants pid +
        # pc_eid, so read the FHIR Appointment for its patient first.
        try:
            resp = await self._client.get(
                f"{self._base}/Appointment/{hms_booking_id}", headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"cancel lookup: {exc}") from exc
        if resp.status_code == 404:
            return CancelResult(status="NOT_FOUND")
        if resp.status_code >= 400:
            raise TransientError(f"cancel lookup HTTP {resp.status_code}")
        patient_ref = self._actor(resp.json(), "Patient")
        pid = await self._pid(patient_ref)
        rows = self._data(await self._std_request("GET", f"/patient/{pid}/appointment")) or []
        match = [r for r in rows if str(r.get("pc_uuid")) == hms_booking_id]
        if not match:
            return CancelResult(status="NOT_FOUND")
        d = await self._std_request("DELETE", f"/patient/{pid}/appointment/{match[0]['pc_eid']}")
        if d.status_code >= 400:
            raise TransientError(f"OpenEMR delete HTTP {d.status_code}: {d.text[:200]}")
        return CancelResult(status="SUCCESS")

    async def push_visit_event(
        self, event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized
    ) -> None:
        raise WriteNotSupported("OpenEMR has no appointment status update route")
