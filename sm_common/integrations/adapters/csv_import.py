from __future__ import annotations

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
from sm_common.integrations.hms_adapter import HmsAdapter


class CsvImportAdapter(HmsAdapter):
    """CSV batch import adapter for Tier 4 HMS (eHospital@NIC, manual bridges).

    Implement when a signed pilot requires CSV ingestion.

    STATUS (2026-06-12): placeholder — no service consumes this yet and all
    methods raise NotImplementedError. Implement when a pilot contract requires
    CSV import; delete if CSV ingestion is dropped from the roadmap.
    """

    vendor_name = "csv_import"

    async def find_patient(self, **kwargs) -> CanonicalPatient | None:  # type: ignore[override]
        raise NotImplementedError("implement when pilot requires CSV import")

    async def list_appointments_modified_since(
        self, cursor: str, until_date: date
    ) -> tuple[list[CanonicalAppointment], str]:
        raise NotImplementedError("implement when pilot requires CSV import")

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        raise NotImplementedError("implement when pilot requires CSV import")

    async def fetch_recent_bookings(
        self, hospital_id: UUID, lookback_minutes: int
    ) -> list[ExternalBooking]:
        raise NotImplementedError("implement when pilot requires CSV import")

    async def write_back_idempotent(
        self, booking_id: UUID, payload: dict, idempotency_key: str
    ) -> WriteBackResult:  # type: ignore[type-arg]
        raise NotImplementedError("implement when pilot requires CSV import")

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        raise NotImplementedError("implement when pilot requires CSV import")

    async def push_visit_event(
        self, event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized
    ) -> None:
        raise NotImplementedError("implement when pilot requires CSV import")

    async def health_check(self) -> AdapterHealth:
        raise NotImplementedError("implement when pilot requires CSV import")
