from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

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


class HmsAdapter(ABC):
    """Abstract base class every vendor-specific HMS connector must implement."""

    vendor_name: str

    # ─── Inbound (HMS → QueueCare) ───────────────────────────────────────────

    @abstractmethod
    async def find_patient(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
    ) -> CanonicalPatient | None:
        """Return the patient record if HMS knows them, else None.

        ``phone`` is the plaintext number. ``phone_hash`` is our own local
        join-space index and matches nothing on any vendor system — callers
        holding only the hash get None back.
        """

    async def get_patient(self, external_id: str) -> CanonicalPatient | None:
        """Read a patient by the vendor's own resource id.

        Deliberately not abstract: bahmni, mocdoc, generic_rest and csv_import
        have no direct-read route and would all break. They inherit this default
        and callers fall back to ``find_patient``.
        """
        return None

    async def search_patients(
        self,
        phone_hash: str | None = None,
        mrn: str | None = None,
        abha_id: str | None = None,
        phone: str | None = None,
    ) -> list[CanonicalPatient]:
        """Every candidate the HMS returns. Matching needs ALL of them: one
        phone commonly serves a household, and ambiguity can only be seen by a
        caller that sees every hit.

        Default for adapters with no list-returning search: wraps
        ``find_patient``. Such an adapter cannot express ambiguity — kiosk
        matching is only as safe as the adapter's own search.
        """
        found = await self.find_patient(
            phone_hash=phone_hash, mrn=mrn, abha_id=abha_id, phone=phone
        )
        return [found] if found is not None else []

    @abstractmethod
    async def list_appointments_modified_since(
        self,
        cursor: str,
        until_date: date,
    ) -> tuple[list[CanonicalAppointment], str]:
        """Return (appointments, new_cursor). Used by Celery polling loop."""

    @abstractmethod
    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        """Full doctor list with denormalised speciality/department. Called nightly."""

    @abstractmethod
    async def fetch_recent_bookings(
        self,
        hospital_id: UUID,
        lookback_minutes: int,
    ) -> list[ExternalBooking]:
        """Recent bookings for reconciliation worker drift detection."""

    # ─── Outbound (QueueCare → HMS) ──────────────────────────────────────────

    @abstractmethod
    async def write_back_idempotent(self, write: AppointmentWrite) -> WriteBackResult:
        """Write a booking to the HMS. Idempotent on ``write.booking_id``.

        Raises ConflictError (slot taken / duplicate found), TransientError
        (retry), AuthError, WriteNotSupported or VendorRejected (terminal).
        """

    @abstractmethod
    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        """Saga compensation: cancel a booking previously written via write_back_idempotent."""

    @abstractmethod
    async def push_visit_event(
        self,
        event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized,
    ) -> None:
        """Write lifecycle events back to HMS. Idempotent on event.event_uuid."""

    # ─── Health ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def health_check(self) -> AdapterHealth:
        """Report auth status, last successful call, and latency."""
