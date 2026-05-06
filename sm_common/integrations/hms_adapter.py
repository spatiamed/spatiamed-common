from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

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
    ) -> CanonicalPatient | None:
        """Return the patient record if HMS knows them, else None."""

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
    async def write_back_idempotent(
        self,
        booking_id: UUID,
        payload: dict,  # type: ignore[type-arg]
        idempotency_key: str,
    ) -> WriteBackResult:
        """Write a booking to HMS. Must be idempotent on idempotency_key."""

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
