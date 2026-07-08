"""Tests for sm_common.events — the shared QueueCare <-> CareLoop event registry.

Every payload sample below is copied from the LIVE producer/consumer code so the
models are pinned to the real contract, not an invented one:

* ``visit.*``            -> QueueCare ``server/app/routers/tokens.py``
* ``report.*``           -> QueueCare ``server/app/services/clinical/notes_signoff.py``
* ``consultation.scheduled`` -> QueueCare
  ``server/app/services/clinical/telemedicine/encounter_service.py``
* ``patient.*``          -> QueueCare
  ``server/app/integration/careloop_handlers.py`` (consumer of CareLoop output)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sm_common import (
    CURRENT_ENVELOPE_VERSION,
    EVENT_PAYLOAD_MODELS,
    EventEnvelope,
    EventSource,
    EventType,
    build_envelope,
    parse_envelope,
    parse_payload,
    payload_model_for,
)
from sm_common.events import (
    ConsultationScheduledPayload,
    PatientAppointmentCancelledPayload,
    PatientPreRegisteredPayload,
    ReportReadyPayload,
    ReportRevokedPayload,
    VisitCancelledPayload,
    VisitCompletedPayload,
    VisitNoShowPayload,
    VisitTokenIssuedPayload,
)

# --- real samples lifted verbatim from producer/consumer code ---------------

VISIT_TOKEN_ISSUED_SAMPLE = {
    "phone_hash": "abc123hash",
    "token_number": "CARD-001",
    "department_code": "CARDIO",
    "department_name": "Cardiology",
    "doctor_name": "Dr. Rao",
    "priority": "normal",
    "queue_position": 3,
    "estimated_wait_minutes": 30,
    "visit_id": str(uuid.uuid4()),
    "pre_registration_id": str(uuid.uuid4()),
    "is_pre_registered": True,
}

VISIT_COMPLETED_SAMPLE = {
    "phone_hash": "abc123hash",
    "token_number": "CARD-001",
    "department": "Cardiology",
    "doctor": "Dr. Rao",
    "consultation_duration_minutes": 12,
    "revenue_paise": 50000,
    "visit_id": str(uuid.uuid4()),
}

VISIT_NO_SHOW_SAMPLE = {
    "phone_hash": "abc123hash",
    "token_number": "CARD-001",
    "department_code": "CARDIO",
    "doctor_name": "Dr. Rao",
    "was_pre_registered": False,
    "trigger_source": "manual",
}

REPORT_READY_SAMPLE = {
    "report_id": str(uuid.uuid4()),
    "clinical_note_id": str(uuid.uuid4()),
    "hospital_id": str(uuid.uuid4()),
    "patient_phone_hash": "abc123hash",
    "visit_id": str(uuid.uuid4()),
    "channels": ["whatsapp", "email"],
    "document_type": "clinical_report",
}

REPORT_REVOKED_SAMPLE = {
    "report_id": str(uuid.uuid4()),
    "clinical_note_id": str(uuid.uuid4()),
    "hospital_id": str(uuid.uuid4()),
    "patient_phone_hash": "abc123hash",
    "revoked_by": str(uuid.uuid4()),
    "revoke_reason": "superseded",
}

CONSULTATION_SCHEDULED_SAMPLE = {
    "consultation_id": str(uuid.uuid4()),
    "booking_id": str(uuid.uuid4()),
    "hospital_id": str(uuid.uuid4()),
    "patient_phone_hash": "abc123hash",
    "join_url": "https://visit.example.com/room/xyz",
    "room_ref": "room-xyz",
    "provider": "careloop_rtc",
    "scheduled_start": "2026-07-08T10:00:00+00:00",
    "channels": ["whatsapp", "email"],
}

PATIENT_PRE_REGISTERED_SAMPLE = {
    "hospital_id": str(uuid.uuid4()),
    "appointment_date": "2026-07-09",
    "phone_hash": "abc123hash",
    "preferred_department_id": str(uuid.uuid4()),
    "preferred_doctor_id": str(uuid.uuid4()),
    "source_channel": "instagram",
    "source_campaign": "monsoon",
    "lead_id": "lead-1",
    "referral_doctor_id": str(uuid.uuid4()),
    "preferred_language": "hi",
}

PATIENT_APPOINTMENT_CANCELLED_SAMPLE = {
    "hospital_id": str(uuid.uuid4()),
    "phone_hash": "abc123hash",
    "appointment_date": "2026-07-09",
}

SAMPLES: dict[EventType, dict] = {
    EventType.VISIT_TOKEN_ISSUED: VISIT_TOKEN_ISSUED_SAMPLE,
    EventType.VISIT_COMPLETED: VISIT_COMPLETED_SAMPLE,
    EventType.VISIT_NO_SHOW: VISIT_NO_SHOW_SAMPLE,
    EventType.VISIT_CANCELLED: {"phone_hash": "abc123hash", "token_number": "CARD-001"},
    EventType.REPORT_READY: REPORT_READY_SAMPLE,
    EventType.REPORT_REVOKED: REPORT_REVOKED_SAMPLE,
    EventType.CONSULTATION_SCHEDULED: CONSULTATION_SCHEDULED_SAMPLE,
    EventType.PATIENT_PRE_REGISTERED: PATIENT_PRE_REGISTERED_SAMPLE,
    EventType.PATIENT_APPOINTMENT_CANCELLED: PATIENT_APPOINTMENT_CANCELLED_SAMPLE,
}


# --- enum coverage ----------------------------------------------------------


def test_event_type_values() -> None:
    assert {e.value for e in EventType} == {
        "visit.token_issued",
        "visit.completed",
        "visit.no_show",
        "visit.cancelled",
        "report.ready",
        "report.revoked",
        "consultation.scheduled",
        "patient.pre_registered",
        "patient.appointment_cancelled",
    }


def test_event_type_is_str() -> None:
    # StrEnum members compare equal to their string value (drop-in for literals).
    assert EventType.VISIT_COMPLETED == "visit.completed"


def test_every_event_type_has_a_payload_model() -> None:
    assert set(EVENT_PAYLOAD_MODELS) == set(EventType)
    assert len(EVENT_PAYLOAD_MODELS) == 9


def test_every_sample_covers_every_event() -> None:
    assert set(SAMPLES) == set(EventType)


# --- each payload model validates a real sample -----------------------------


@pytest.mark.parametrize("event_type", list(EventType))
def test_payload_model_validates_real_sample(event_type: EventType) -> None:
    model = payload_model_for(event_type)
    obj = model.model_validate(SAMPLES[event_type])
    # round-trip: the model re-serializes to a superset-compatible dict.
    dumped = obj.model_dump(mode="json")
    for key, value in SAMPLES[event_type].items():
        assert dumped[key] == value


def test_specific_model_fields() -> None:
    completed = VisitCompletedPayload.model_validate(VISIT_COMPLETED_SAMPLE)
    assert completed.revenue_paise == 50000
    assert completed.department == "Cardiology"
    assert completed.doctor == "Dr. Rao"

    token = VisitTokenIssuedPayload.model_validate(VISIT_TOKEN_ISSUED_SAMPLE)
    assert token.queue_position == 3
    assert token.is_pre_registered is True

    prereg = PatientPreRegisteredPayload.model_validate(PATIENT_PRE_REGISTERED_SAMPLE)
    assert prereg.preferred_language == "hi"

    cancel = PatientAppointmentCancelledPayload.model_validate(PATIENT_APPOINTMENT_CANCELLED_SAMPLE)
    assert cancel.appointment_date == "2026-07-09"

    report = ReportReadyPayload.model_validate(REPORT_READY_SAMPLE)
    assert report.channels == ["whatsapp", "email"]

    revoked = ReportRevokedPayload.model_validate(REPORT_REVOKED_SAMPLE)
    assert revoked.revoke_reason == "superseded"

    consult = ConsultationScheduledPayload.model_validate(CONSULTATION_SCHEDULED_SAMPLE)
    assert consult.provider == "careloop_rtc"

    noshow = VisitNoShowPayload.model_validate(VISIT_NO_SHOW_SAMPLE)
    assert noshow.trigger_source == "manual"

    cancelled_visit = VisitCancelledPayload.model_validate(SAMPLES[EventType.VISIT_CANCELLED])
    assert cancelled_visit.phone_hash == "abc123hash"


def test_visit_completed_legacy_trio_removed() -> None:
    """Regression for #17 (v0.6.0): the legacy ``department_code`` /
    ``department_name`` / ``doctor_name`` fields are GONE from
    ``VisitCompletedPayload``. They were ``""``-defaults no producer emitted
    (since QueueCare #99) and no consumer ever read, but their presence forced
    QueueCare to ``exclude=`` them at every ``visit.completed`` emit site because
    ``build_envelope`` does an unconditional ``model_dump``. Pinning their
    absence here so the trio cannot quietly return.
    """
    fields = VisitCompletedPayload.model_fields
    for gone in ("department_code", "department_name", "doctor_name"):
        assert gone not in fields, f"{gone} must stay removed from VisitCompletedPayload"

    # And because the model is extra="forbid", a producer still emitting the
    # trio (the pre-#99 shape) is now rejected rather than silently accepted.
    bad = dict(VISIT_COMPLETED_SAMPLE)
    bad["department_code"] = "CARDIO"
    with pytest.raises(ValidationError):
        VisitCompletedPayload.model_validate(bad)


# --- mismatch => validation error -------------------------------------------


def test_missing_required_field_raises() -> None:
    bad = dict(VISIT_COMPLETED_SAMPLE)
    del bad["revenue_paise"]  # consumer-required field
    with pytest.raises(ValidationError):
        VisitCompletedPayload.model_validate(bad)


def test_unknown_field_is_rejected_extra_forbid() -> None:
    # A producer that renames/adds a field is caught (the XR-05 guarantee).
    bad = dict(VISIT_COMPLETED_SAMPLE)
    bad["revenue_cents"] = 500  # renamed field -> extra -> forbidden
    with pytest.raises(ValidationError):
        VisitCompletedPayload.model_validate(bad)


def test_wrong_type_raises() -> None:
    bad = dict(VISIT_TOKEN_ISSUED_SAMPLE)
    bad["queue_position"] = "not-an-int"  # int field given a non-numeric str
    with pytest.raises(ValidationError):
        VisitTokenIssuedPayload.model_validate(bad)


def test_token_number_and_priority_are_strings() -> None:
    """Regression for #15 (v0.5.1): the live QueueCare producer emits
    ``Token.token_number`` (``String(20)``, e.g. ``"CARD-001"``) and
    ``Token.priority`` (``String``, e.g. ``"normal"``). Typing them as ``int``
    made the models raise ValidationError on real payloads, so the flagship
    revenue event (visit.completed) could not be built via the registry.
    """
    # The real string shapes validate cleanly.
    token = VisitTokenIssuedPayload.model_validate(VISIT_TOKEN_ISSUED_SAMPLE)
    assert token.token_number == "CARD-001"
    assert token.priority == "normal"

    completed = VisitCompletedPayload.model_validate(VISIT_COMPLETED_SAMPLE)
    assert completed.token_number == "CARD-001"

    noshow = VisitNoShowPayload.model_validate(VISIT_NO_SHOW_SAMPLE)
    assert noshow.token_number == "CARD-001"

    cancelled = VisitCancelledPayload.model_validate(SAMPLES[EventType.VISIT_CANCELLED])
    assert cancelled.token_number == "CARD-001"

    # And integers are now rejected (pydantic v2 does not coerce int -> str),
    # pinning the fields as str so an accidental revert is caught.
    for field in ("token_number", "priority"):
        bad = dict(VISIT_TOKEN_ISSUED_SAMPLE)
        bad[field] = 42
        with pytest.raises(ValidationError):
            VisitTokenIssuedPayload.model_validate(bad)


# --- envelope round-trip with envelope_version ------------------------------


def test_current_envelope_version_is_one() -> None:
    assert CURRENT_ENVELOPE_VERSION == 1


def test_build_envelope_round_trip() -> None:
    payload = VisitCompletedPayload.model_validate(VISIT_COMPLETED_SAMPLE)
    tenant = str(uuid.uuid4())
    env = build_envelope(
        event_type=EventType.VISIT_COMPLETED,
        payload=payload,
        source=EventSource.QUEUECARE,
        tenant_id=tenant,
    )
    assert env.envelope_version == CURRENT_ENVELOPE_VERSION
    assert env.event_type == EventType.VISIT_COMPLETED
    assert env.source == EventSource.QUEUECARE
    assert env.tenant_id == tenant
    assert isinstance(env.event_id, uuid.UUID)

    # serialize -> wire dict -> parse back
    wire = env.model_dump(mode="json")
    assert wire["envelope_version"] == 1
    assert wire["source"] == "queuecare"
    assert wire["event_type"] == "visit.completed"

    reparsed = parse_envelope(wire)
    assert reparsed == env

    # data validates back into the typed payload
    got = parse_payload(reparsed)
    assert isinstance(got, VisitCompletedPayload)
    assert got.revenue_paise == 50000


def test_parse_payload_rejects_mismatched_data() -> None:
    # An envelope whose data is missing a required field fails at parse time,
    # not silently — this is the whole point of the registry.
    env = EventEnvelope(
        event_id=uuid.uuid4(),
        event_type=EventType.VISIT_COMPLETED,
        timestamp=datetime.now(UTC),
        source=EventSource.QUEUECARE,
        tenant_id=str(uuid.uuid4()),
        data={"phone_hash": "x", "visit_id": "v"},  # no revenue_paise/token_number
    )
    with pytest.raises(ValidationError):
        parse_payload(env)


def test_build_envelope_rejects_wrong_payload_type() -> None:
    wrong = VisitNoShowPayload.model_validate(VISIT_NO_SHOW_SAMPLE)
    with pytest.raises(TypeError):
        build_envelope(
            event_type=EventType.VISIT_COMPLETED,
            payload=wrong,
            source=EventSource.QUEUECARE,
        )


def test_parse_envelope_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        parse_envelope(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "visit.exploded",
                "timestamp": datetime.now(UTC).isoformat(),
                "source": "queuecare",
                "data": {},
            }
        )


def test_envelope_defaults_version_when_absent() -> None:
    env = parse_envelope(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "patient.appointment_cancelled",
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "careloop",
            "data": PATIENT_APPOINTMENT_CANCELLED_SAMPLE,
        }
    )
    # no envelope_version on the wire (legacy producer) -> current default
    assert env.envelope_version == CURRENT_ENVELOPE_VERSION
    assert env.source == EventSource.CARELOOP
