"""Shared event registry for the QueueCare <-> CareLoop webhook contract.

This module is the single source of truth for every event that crosses the
QueueCare/CareLoop webhook boundary in either direction. Before this existed,
the event-type strings and their payload shapes were inline literals duplicated
across at least four places (two QueueCare producer modules, CareLoop's live
``if event_type ==`` dispatch chain, QueueCare's reverse-direction ``match``,
plus a dead-and-drifted ``KNOWN_EVENTS`` frozenset in CareLoop) with no shared
schema. A producer field rename was therefore an *unsignalled* breaking change
that failed silently in production (see the ``visit.completed`` drift, XR-01 /
XR-08).

The design goal is that a producer/consumer field mismatch becomes a *type /
validation error* at the boundary instead of a silent no-op:

* :class:`EventType` enumerates every event string used in BOTH directions.
* One :class:`EventPayload` subclass per event models the *intended* live
  contract. Payload models are ``extra="forbid"`` so an unknown or renamed
  field is loud, not silently dropped.
* :data:`EVENT_PAYLOAD_MODELS` maps each :class:`EventType` to its payload
  model. :func:`build_envelope` / :func:`parse_payload` use it so that
  building or parsing an envelope validates ``data`` against the mapped model.
* :class:`EventEnvelope` carries the shared wire envelope PLUS a new
  :data:`CURRENT_ENVELOPE_VERSION` (:attr:`EventEnvelope.envelope_version`) so a
  schema evolution is explicit on the wire.

Adoption note: QueueCare (producer) and CareLoop (consumer) both already import
``sm_common``; both should replace their inline literals + hand-rolled envelope
dict with these types. See ``.report-events.md`` for the per-repo checklist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CURRENT_ENVELOPE_VERSION",
    "EVENT_PAYLOAD_MODELS",
    "ConsultationScheduledPayload",
    "EventEnvelope",
    "EventPayload",
    "EventSource",
    "EventType",
    "PatientAppointmentCancelledPayload",
    "PatientPreRegisteredPayload",
    "ReportReadyPayload",
    "ReportRevokedPayload",
    "VisitCancelledPayload",
    "VisitCompletedPayload",
    "VisitNoShowPayload",
    "VisitTokenIssuedPayload",
    "build_envelope",
    "parse_envelope",
    "parse_payload",
    "payload_model_for",
]


# ---------------------------------------------------------------------------
# Envelope version
# ---------------------------------------------------------------------------
#: Wire version of :class:`EventEnvelope`. Bump when the envelope shape (the
#: fields OUTSIDE ``data``) changes in a non-additive way. Payload-model changes
#: are versioned per-event by the model itself, not by this number.
CURRENT_ENVELOPE_VERSION: int = 1


class EventSource(StrEnum):
    """Which service produced an envelope. Stamped on :attr:`EventEnvelope.source`."""

    QUEUECARE = "queuecare"
    CARELOOP = "careloop"


class EventType(StrEnum):
    """Every event-type string on the QueueCare <-> CareLoop webhook contract.

    Forward direction (QueueCare -> CareLoop): ``visit.*``, ``report.*``,
    ``consultation.scheduled``. Reverse direction (CareLoop -> QueueCare):
    ``patient.*``.
    """

    # QueueCare -> CareLoop (forward)
    VISIT_TOKEN_ISSUED = "visit.token_issued"
    VISIT_COMPLETED = "visit.completed"
    VISIT_NO_SHOW = "visit.no_show"
    VISIT_CANCELLED = "visit.cancelled"
    REPORT_READY = "report.ready"
    REPORT_REVOKED = "report.revoked"
    CONSULTATION_SCHEDULED = "consultation.scheduled"

    # CareLoop -> QueueCare (reverse)
    PATIENT_PRE_REGISTERED = "patient.pre_registered"
    PATIENT_APPOINTMENT_CANCELLED = "patient.appointment_cancelled"


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------
class EventPayload(BaseModel):
    """Base for every per-event payload model.

    ``extra="forbid"`` is deliberate: a producer that renames or adds a field
    without updating the shared model gets a validation error at the boundary
    instead of the consumer silently reading ``None``. That is the whole point
    of the registry (XR-05).
    """

    model_config = ConfigDict(extra="forbid")


class VisitTokenIssuedPayload(EventPayload):
    """``visit.token_issued`` — QueueCare token created; CareLoop referral-arrival.

    Source of truth (producer): QueueCare ``server/app/routers/tokens.py``
    (``queue_event("visit.token_issued", ...)``). Consumer:
    CareLoop ``app/routers/webhooks.py::_handle_visit_token_issued`` (reads only
    ``visit_id`` + ``phone_hash``; ``tenant_id`` comes from the envelope).
    """

    phone_hash: str
    token_number: int
    department_code: str
    department_name: str
    doctor_name: str = ""
    priority: int
    queue_position: int
    estimated_wait_minutes: int
    visit_id: str
    pre_registration_id: str | None = None
    is_pre_registered: bool = False


class VisitCompletedPayload(EventPayload):
    """``visit.completed`` — visit closed; CareLoop revenue attribution.

    Source of truth (producer): QueueCare ``server/app/routers/tokens.py``
    (``queue_event("visit.completed", ...)``). Consumer:
    CareLoop ``app/routers/webhooks.py::_handle_visit_completed`` (requires
    ``visit_id``, ``phone_hash``, ``revenue_paise``; also reads
    ``department`` / ``doctor``; ``tenant_id`` from the envelope).

    XR-01 / XR-08: the consumer-expected fields are ``revenue_paise`` (paise,
    int), ``department`` and ``doctor`` (human-readable). This is the migration
    target. The legacy ``*_code`` / ``*_name`` fields below are still emitted by
    the current producer for "any other consumer" but NO live consumer reads
    them — they are drift-debt to remove once producers adopt this model.
    """

    phone_hash: str
    token_number: int
    visit_id: str
    revenue_paise: int
    department: str = ""
    doctor: str = ""
    consultation_duration_minutes: int = 0
    # --- legacy / drifted, no live consumer reads these (XR-08) ---
    department_code: str = ""
    department_name: str = ""
    doctor_name: str = ""


class VisitNoShowPayload(EventPayload):
    """``visit.no_show`` — patient did not show; CareLoop no-show rescue.

    Source of truth (producer): QueueCare ``server/app/routers/tokens.py``
    (``queue_event("visit.no_show", ...)``). Consumer:
    CareLoop ``app/routers/webhooks.py::_handle_visit_no_show`` (reads only
    ``phone_hash``; ``tenant_id`` + ``event_id`` from the envelope).
    """

    phone_hash: str
    token_number: int
    department_code: str = ""
    doctor_name: str = ""
    was_pre_registered: bool = False
    trigger_source: str = "manual"


class VisitCancelledPayload(EventPayload):
    """``visit.cancelled`` — visit cancelled; CareLoop pauses sequences.

    Consumer: CareLoop ``app/routers/webhooks.py::_handle_visit_cancelled``
    (reads ``phone_hash``; ``tenant_id`` + ``event_id`` from the envelope).

    NOTE (XR-01, dead-on-arrival): NO QueueCare producer currently emits
    ``visit.cancelled`` — the consumer handler and the dead ``KNOWN_EVENTS``
    frozenset both list it but nothing produces it. This model captures the
    intended shape so the producer can be added against a real contract.
    """

    phone_hash: str
    token_number: int | None = None
    reason: str | None = None


class ReportReadyPayload(EventPayload):
    """``report.ready`` — signed clinical report; CareLoop delivers to patient.

    Source of truth (producer): QueueCare
    ``server/app/services/clinical/notes_signoff.py``
    (``queue_event("report.ready", ...)``). Consumer:
    CareLoop ``app/services/clinical_delivery/orchestrator.py::deliver_report``
    (requires ``hospital_id``, ``patient_phone_hash``, ``report_id``; reads
    ``channels`` + ``document_type``).

    NOTE: unlike ``visit.*``, this family scopes tenancy via ``hospital_id`` and
    ``patient_phone_hash`` INSIDE ``data`` (not the envelope ``tenant_id`` /
    ``phone_hash``). Flagged for consolidation at adoption.
    """

    report_id: str
    clinical_note_id: str
    hospital_id: str
    patient_phone_hash: str
    visit_id: str
    channels: list[str] = Field(default_factory=list)
    document_type: str = "clinical_report"


class ReportRevokedPayload(EventPayload):
    """``report.revoked`` — report retracted; CareLoop notifies patient to disregard.

    Source of truth (producer): QueueCare
    ``server/app/services/clinical/notes_signoff.py``
    (``queue_event("report.revoked", ...)``). Consumer:
    CareLoop ``app/services/clinical_delivery/orchestrator.py::revoke_report``
    (reads ``hospital_id`` + ``patient_phone_hash``).
    """

    report_id: str
    clinical_note_id: str
    hospital_id: str
    patient_phone_hash: str
    revoked_by: str
    revoke_reason: str


class ConsultationScheduledPayload(EventPayload):
    """``consultation.scheduled`` — video consult provisioned; CareLoop sends join link.

    Source of truth (producer): QueueCare
    ``server/app/services/clinical/telemedicine/encounter_service.py``
    (``queue_event("consultation.scheduled", ...)``). Consumer:
    CareLoop ``app/services/clinical_delivery/consultation_notify.py::
    deliver_consultation_link`` (requires ``hospital_id``,
    ``patient_phone_hash``, ``join_url``; reads ``channels`` / ``provider`` /
    ``room_ref``).
    """

    consultation_id: str
    booking_id: str
    hospital_id: str
    patient_phone_hash: str
    join_url: str
    room_ref: str | None = None
    provider: str
    scheduled_start: str
    channels: list[str] = Field(default_factory=list)


class PatientPreRegisteredPayload(EventPayload):
    """``patient.pre_registered`` — CareLoop -> QueueCare pre-registration.

    Source of truth (consumer): QueueCare
    ``server/app/integration/careloop_handlers.py::handle_pre_registration``.
    Requires ``hospital_id``, ``appointment_date``, and ``phone_hash`` (the
    latter is optional only when ``phone_encrypted`` is supplied and QueueCare
    can recompute it). Producer lives in CareLoop.
    """

    hospital_id: str
    appointment_date: str
    phone_hash: str
    patient_name_encrypted: str | None = None
    phone_encrypted: str | None = None
    preferred_department_id: str | None = None
    preferred_doctor_id: str | None = None
    source_channel: str | None = None
    source_campaign: str | None = None
    lead_id: str | None = None
    referral_doctor_id: str | None = None
    preferred_language: str = "en"


class PatientAppointmentCancelledPayload(EventPayload):
    """``patient.appointment_cancelled`` — CareLoop -> QueueCare cancellation.

    Source of truth (consumer): QueueCare
    ``server/app/integration/careloop_handlers.py::handle_cancellation``
    (requires ``hospital_id``, ``phone_hash``, ``appointment_date``).
    """

    hospital_id: str
    phone_hash: str
    appointment_date: str


#: Canonical EventType -> payload-model mapping. Building/parsing an envelope
#: validates ``data`` against the mapped model, so a field mismatch is a
#: ValidationError rather than a silent no-op.
EVENT_PAYLOAD_MODELS: dict[EventType, type[EventPayload]] = {
    EventType.VISIT_TOKEN_ISSUED: VisitTokenIssuedPayload,
    EventType.VISIT_COMPLETED: VisitCompletedPayload,
    EventType.VISIT_NO_SHOW: VisitNoShowPayload,
    EventType.VISIT_CANCELLED: VisitCancelledPayload,
    EventType.REPORT_READY: ReportReadyPayload,
    EventType.REPORT_REVOKED: ReportRevokedPayload,
    EventType.CONSULTATION_SCHEDULED: ConsultationScheduledPayload,
    EventType.PATIENT_PRE_REGISTERED: PatientPreRegisteredPayload,
    EventType.PATIENT_APPOINTMENT_CANCELLED: PatientAppointmentCancelledPayload,
}


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------
class EventEnvelope(BaseModel):
    """The shared wire envelope for every cross-service event.

    Mirrors the live envelope written by QueueCare's
    ``app/integration/webhook_sender.py`` (``event_id``, ``event_type``,
    ``timestamp``, ``source``, ``tenant_id``, ``data``) and ADDS
    :attr:`envelope_version` (the XR-05 schema-version field) so a shape change
    is explicit on the wire.

    ``tenant_id`` is optional at the type level (the reverse-direction
    ``patient.*`` events scope tenancy via ``hospital_id`` inside ``data``), but
    the forward ``visit.*`` contract REQUIRES it — CareLoop drops events without
    it (XR-01). ``data`` is left as a raw mapping here; use
    :func:`parse_payload` to validate it against the event's model.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: EventType
    timestamp: datetime
    source: EventSource
    envelope_version: int = CURRENT_ENVELOPE_VERSION
    tenant_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


PayloadT = TypeVar("PayloadT", bound=EventPayload)


def payload_model_for(event_type: EventType) -> type[EventPayload]:
    """Return the payload model class registered for ``event_type``."""

    return EVENT_PAYLOAD_MODELS[event_type]


def build_envelope(
    *,
    event_type: EventType,
    payload: EventPayload,
    source: EventSource,
    tenant_id: str | None = None,
    event_id: uuid.UUID | None = None,
    timestamp: datetime | None = None,
    envelope_version: int = CURRENT_ENVELOPE_VERSION,
) -> EventEnvelope:
    """Build a validated :class:`EventEnvelope` from a typed payload.

    Passing a payload whose type does not match the model registered for
    ``event_type`` raises :class:`TypeError` — this is what makes a
    producer/consumer field mismatch fail loudly at construction time.
    """

    expected = EVENT_PAYLOAD_MODELS[event_type]
    if not isinstance(payload, expected):
        raise TypeError(
            f"payload for {event_type.value!r} must be {expected.__name__}, "
            f"got {type(payload).__name__}"
        )
    return EventEnvelope(
        event_id=event_id if event_id is not None else uuid.uuid4(),
        event_type=event_type,
        timestamp=timestamp if timestamp is not None else datetime.now(UTC),
        source=source,
        envelope_version=envelope_version,
        tenant_id=tenant_id,
        data=payload.model_dump(mode="json"),
    )


def parse_envelope(raw: dict[str, Any]) -> EventEnvelope:
    """Validate a raw wire dict into an :class:`EventEnvelope`.

    An unknown ``event_type`` string raises a ``ValidationError`` (it is not in
    :class:`EventType`), so an unrecognised event cannot be silently accepted.
    """

    return EventEnvelope.model_validate(raw)


def parse_payload(envelope: EventEnvelope) -> EventPayload:
    """Validate ``envelope.data`` against the model for ``envelope.event_type``.

    Raises ``pydantic.ValidationError`` when the producer's fields do not match
    the shared contract — the XR-05 guarantee that a mismatch is a validation
    error, not a silent no-op.
    """

    model = EVENT_PAYLOAD_MODELS[envelope.event_type]
    return model.model_validate(envelope.data)
