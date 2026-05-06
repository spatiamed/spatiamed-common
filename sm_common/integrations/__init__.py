from sm_common.integrations.canonical_types import (
    AppointmentCreated,
    AppointmentRescheduled,
    AppointmentCancelled,
    VisitCheckedIn,
    VisitConsultationStarted,
    VisitFinalized,
    CanonicalPatient,
    CanonicalDoctor,
    CanonicalAppointment,
    WriteBackResult,
    CancelResult,
    AdapterHealth,
    ExternalBooking,
)
from sm_common.integrations.hms_adapter import HmsAdapter
from sm_common.integrations.exceptions import (
    HmsAdapterError,
    ConflictError,
    TransientError,
    AuthError,
)

__all__ = [
    "HmsAdapter",
    "AppointmentCreated", "AppointmentRescheduled", "AppointmentCancelled",
    "VisitCheckedIn", "VisitConsultationStarted", "VisitFinalized",
    "CanonicalPatient", "CanonicalDoctor", "CanonicalAppointment",
    "WriteBackResult", "CancelResult", "AdapterHealth", "ExternalBooking",
    "HmsAdapterError", "ConflictError", "TransientError", "AuthError",
]
