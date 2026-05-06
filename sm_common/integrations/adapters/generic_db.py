from __future__ import annotations

from datetime import date
from uuid import UUID

from sm_common.integrations.canonical_types import (
    AdapterHealth, CancelResult, CanonicalAppointment, CanonicalDoctor,
    CanonicalPatient, ExternalBooking, VisitCheckedIn,
    VisitConsultationStarted, VisitFinalized, WriteBackResult,
)
from sm_common.integrations.hms_adapter import HmsAdapter


class GenericDirectDbAdapter(HmsAdapter):
    """Read-only SQL adapter for Tier 3 on-prem HMS (Birlamedisoft, Akhil HIS, eHospital).

    Implement when a signed pilot requires direct DB access.
    Until then, install via hms_integrations.tier = 't3' routes through connector agent.
    """

    vendor_name = "generic_db"

    async def find_patient(self, **kwargs) -> CanonicalPatient | None:  # type: ignore[override]
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def list_appointments_modified_since(self, cursor: str, until_date: date) -> tuple[list[CanonicalAppointment], str]:
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def fetch_doctor_roster(self, as_of_date: date) -> list[CanonicalDoctor]:
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def fetch_recent_bookings(self, hospital_id: UUID, lookback_minutes: int) -> list[ExternalBooking]:
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def write_back_idempotent(self, booking_id: UUID, payload: dict, idempotency_key: str) -> WriteBackResult:  # type: ignore[type-arg]
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def cancel(self, hms_booking_id: str, reason: str) -> CancelResult:
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def push_visit_event(self, event: VisitCheckedIn | VisitConsultationStarted | VisitFinalized) -> None:
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")

    async def health_check(self) -> AdapterHealth:
        raise NotImplementedError("implement when pilot requires Tier 3 direct DB access")
