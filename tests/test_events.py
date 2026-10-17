"""Tests for sm_common.events — the shared QueueCare <-> CareLoop event registry.

Every payload sample below is copied from the LIVE producer/consumer code so the
models are pinned to the real contract, not an invented one:

* ``visit.*``            -> QueueCare ``server/app/routers/tokens.py``
* ``report.*``           -> QueueCare ``server/app/services/clinical/notes_signoff.py``
* ``consultation.scheduled`` -> QueueCare
  ``server/app/services/clinical/telemedicine/encounter_service.py``
* ``booking.updated``    -> contract from CareLoop #289 (consumer
  ``app/services/appointment_reminders.py``); producer QueueCare
* ``patient.*``          -> QueueCare
  ``server/app/integration/careloop_handlers.py`` (consumer of CareLoop output)
"""

from __future__ import annotations

import json
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
    BookingUpdatedPayload,
    ConsultationScheduledPayload,
    EventPayload,
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

BOOKING_UPDATED_SAMPLE = {
    "booking_id": str(uuid.uuid4()),
    "phone_hash": "abc123hash",
    "status": "confirmed",
    "slot_start": "2026-10-05T07:00:00+00:00",
    "doctor_name": "Dr. Rao",
    "updated_at": "2026-10-02T09:15:00.123456+00:00",
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
    EventType.BOOKING_UPDATED: BOOKING_UPDATED_SAMPLE,
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
        "booking.updated",
    }


def test_event_type_is_str() -> None:
    # StrEnum members compare equal to their string value (drop-in for literals).
    assert EventType.VISIT_COMPLETED == "visit.completed"


def test_every_event_type_has_a_payload_model() -> None:
    assert set(EVENT_PAYLOAD_MODELS) == set(EventType)
    assert len(EVENT_PAYLOAD_MODELS) == 10


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


# --- booking.updated (CareLoop #285 appointment reminders) ------------------


def test_booking_updated_contract_fields() -> None:
    """The exact field set CareLoop #289's reminder reconciler reads."""
    assert set(BookingUpdatedPayload.model_fields) == {
        "booking_id",
        "phone_hash",
        "status",
        "slot_start",
        "doctor_name",
        "updated_at",
        # v0.20.0 booking form (all optional, see the booking-form tests below)
        "status_link",
        "lang",
        "visit_type",
        "contact_phone_encrypted",
        # v0.21.0 post-booking AI triage call (optional)
        "triage_call_enabled",
        "triage_call_consent",
    }
    p = BookingUpdatedPayload.model_validate(BOOKING_UPDATED_SAMPLE)
    assert p.status == "confirmed"
    assert p.doctor_name == "Dr. Rao"


def test_booking_updated_slot_and_doctor_are_nullable() -> None:
    # A booking whose slot was released (or one with no doctor yet) still
    # emits: a null slot is what tells CareLoop to cancel pending reminders.
    p = BookingUpdatedPayload.model_validate(
        {**BOOKING_UPDATED_SAMPLE, "slot_start": None, "doctor_name": None}
    )
    assert p.slot_start is None
    assert p.doctor_name is None


@pytest.mark.parametrize("field", ["booking_id", "phone_hash", "status", "updated_at"])
def test_booking_updated_required_fields(field: str) -> None:
    data = {k: v for k, v in BOOKING_UPDATED_SAMPLE.items() if k != field}
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate(data)


def test_booking_updated_rejects_raw_phone() -> None:
    # PHI guard: the contract carries the phone HASH only. A producer that
    # tries to add the raw number fails at the boundary (extra="forbid").
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate({**BOOKING_UPDATED_SAMPLE, "phone": "+919876543210"})


def test_booking_updated_envelope_round_trip() -> None:
    payload = BookingUpdatedPayload.model_validate(BOOKING_UPDATED_SAMPLE)
    env = build_envelope(
        event_type=EventType.BOOKING_UPDATED,
        payload=payload,
        source=EventSource.QUEUECARE,
        tenant_id="tenant-1",
    )
    wire = env.model_dump(mode="json")
    assert wire["event_type"] == "booking.updated"
    assert wire["data"] == BOOKING_UPDATED_SAMPLE
    assert parse_payload(parse_envelope(wire)) == payload


# --- booking form v1 (v0.20.0): status link, OD-6 fields, OD-1 contact phone ---

STATUS_LINK = "https://book.test/book/demohospital/status/BK-7Q2X#t=opaque-capability"
BOOKING_FORM_PAYLOADS: list[tuple[type[EventPayload], dict[str, object], EventType]] = [
    (
        ConsultationScheduledPayload,
        CONSULTATION_SCHEDULED_SAMPLE,
        EventType.CONSULTATION_SCHEDULED,
    ),
    (BookingUpdatedPayload, BOOKING_UPDATED_SAMPLE, EventType.BOOKING_UPDATED),
]


@pytest.mark.parametrize(("model", "sample", "event_type"), BOOKING_FORM_PAYLOADS)
def test_status_link_is_optional_and_round_trips(
    model: type[EventPayload], sample: dict[str, object], event_type: EventType
) -> None:
    # Old producers (no key) still validate; the default is None.
    old = model.model_validate(sample)
    assert old.status_link is None  # type: ignore[attr-defined]
    assert old.lang is None  # type: ignore[attr-defined]
    assert old.contact_phone_encrypted is None  # type: ignore[attr-defined]

    new = model.model_validate(
        {
            **sample,
            "status_link": STATUS_LINK,
            "lang": "hi",
            "contact_phone_encrypted": "gAAAAopaque",
        }
    )
    wire = build_envelope(
        event_type=event_type, payload=new, source=EventSource.QUEUECARE, tenant_id="t-1"
    ).model_dump(mode="json")
    assert wire["data"]["status_link"] == STATUS_LINK
    assert wire["data"]["lang"] == "hi"
    assert wire["data"]["contact_phone_encrypted"] == "gAAAAopaque"
    assert parse_payload(parse_envelope(wire)) == new

    # extra="forbid" still rejects an unknown key, and a plaintext phone.
    for bad_key in ("status_url", "contact_phone", "phone"):
        with pytest.raises(ValidationError):
            model.model_validate({**sample, bad_key: "x"})


@pytest.mark.parametrize(("model", "sample", "event_type"), BOOKING_FORM_PAYLOADS)
@pytest.mark.parametrize("lang", ["mr", "EN", "english", ""])
def test_lang_only_accepts_en_or_hi(
    model: type[EventPayload], sample: dict[str, object], event_type: EventType, lang: str
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**sample, "lang": lang})


@pytest.mark.parametrize("visit_type", ["video", "in_person"])
def test_booking_updated_visit_type(visit_type: str) -> None:
    p = BookingUpdatedPayload.model_validate({**BOOKING_UPDATED_SAMPLE, "visit_type": visit_type})
    assert p.visit_type == visit_type
    assert BookingUpdatedPayload.model_validate(BOOKING_UPDATED_SAMPLE).visit_type is None
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate({**BOOKING_UPDATED_SAMPLE, "visit_type": "both"})
    # visit_type is a booking.updated field only.
    with pytest.raises(ValidationError):
        ConsultationScheduledPayload.model_validate(
            {**CONSULTATION_SCHEDULED_SAMPLE, "visit_type": visit_type}
        )


@pytest.mark.parametrize("notice_kind", ["confirmed", "rescheduled"])
def test_consultation_scheduled_notice_kind(notice_kind: str) -> None:
    p = ConsultationScheduledPayload.model_validate(
        {**CONSULTATION_SCHEDULED_SAMPLE, "notice_kind": notice_kind}
    )
    assert p.notice_kind == notice_kind
    assert (
        ConsultationScheduledPayload.model_validate(CONSULTATION_SCHEDULED_SAMPLE).notice_kind
        is None
    )
    with pytest.raises(ValidationError):
        ConsultationScheduledPayload.model_validate(
            {**CONSULTATION_SCHEDULED_SAMPLE, "notice_kind": "cancelled"}
        )
    # notice_kind is a consultation.scheduled field only.
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate({**BOOKING_UPDATED_SAMPLE, "notice_kind": notice_kind})


@pytest.mark.parametrize(("model", "sample", "event_type"), BOOKING_FORM_PAYLOADS)
def test_unset_booking_form_fields_stay_off_the_wire(
    model: type[EventPayload], sample: dict[str, object], event_type: EventType
) -> None:
    # A non-form booking serialises exactly as on v0.19.0 (no `status_link: null`),
    # so a consumer pinned to an older sm_common (extra="forbid") still parses it.
    wire = build_envelope(
        event_type=event_type,
        payload=model.model_validate(sample),
        source=EventSource.QUEUECARE,
        tenant_id="t-1",
    ).model_dump(mode="json")
    assert wire["data"] == sample
    # Required nullable fields are NOT dropped.
    if model is BookingUpdatedPayload:
        nulls = {**sample, "slot_start": None, "doctor_name": None}
        assert model.model_validate(nulls).model_dump()["slot_start"] is None
        assert "slot_start" in model.model_validate(nulls).model_dump()


# --- post-booking AI triage call (v0.21.0) ---

# A v0.20.0 form booking payload (no triage keys): what QC sent before v0.21.0.
FORM_BOOKING_UPDATED_V020 = {
    **BOOKING_UPDATED_SAMPLE,
    "status_link": STATUS_LINK,
    "lang": "en",
    "visit_type": "video",
}


@pytest.mark.parametrize(
    ("enabled", "consent"), [(True, True), (True, False), (False, False), (False, True)]
)
def test_booking_updated_triage_fields_serialise(enabled: bool, consent: bool) -> None:
    payload = BookingUpdatedPayload.model_validate(
        {
            **FORM_BOOKING_UPDATED_V020,
            "triage_call_enabled": enabled,
            "triage_call_consent": consent,
        }
    )
    wire = build_envelope(
        event_type=EventType.BOOKING_UPDATED,
        payload=payload,
        source=EventSource.QUEUECARE,
        tenant_id="t-1",
    ).model_dump(mode="json")
    # False is a real value (switch off / consent withdrawn): never dropped.
    assert wire["data"]["triage_call_enabled"] is enabled
    assert wire["data"]["triage_call_consent"] is consent
    assert parse_payload(parse_envelope(wire)) == payload


@pytest.mark.parametrize("sample", [BOOKING_UPDATED_SAMPLE, FORM_BOOKING_UPDATED_V020])
def test_booking_updated_without_triage_fields_is_byte_identical_to_v020(
    sample: dict[str, object],
) -> None:
    payload = BookingUpdatedPayload.model_validate(sample)
    assert payload.triage_call_enabled is None
    assert payload.triage_call_consent is None
    # Byte-identical: same JSON text as v0.20.0 emitted (keys in model order, no nulls).
    assert payload.model_dump_json() == json.dumps(sample, separators=(",", ":"))
    wire = build_envelope(
        event_type=EventType.BOOKING_UPDATED,
        payload=payload,
        source=EventSource.QUEUECARE,
        tenant_id="t-1",
    ).model_dump(mode="json")
    assert wire["data"] == sample
    assert "triage_call_enabled" not in wire["data"]
    assert "triage_call_consent" not in wire["data"]


def test_booking_updated_triage_fields_reject_unknown_and_non_bool() -> None:
    # extra="forbid" still rejects an unknown key next to the new fields.
    for bad_key in ("triage_call", "triage_consent", "triage_call_summary"):
        with pytest.raises(ValidationError):
            BookingUpdatedPayload.model_validate(
                {**FORM_BOOKING_UPDATED_V020, "triage_call_enabled": True, bad_key: True}
            )
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate(
            {**FORM_BOOKING_UPDATED_V020, "triage_call_consent": "maybe"}
        )
    # The triage fields belong to booking.updated only.
    with pytest.raises(ValidationError):
        ConsultationScheduledPayload.model_validate(
            {**CONSULTATION_SCHEDULED_SAMPLE, "triage_call_enabled": True}
        )


# --- v0.22.0: visit.completed patient name + visit language (CareLoop #401) ---


def test_visit_completed_name_and_language_round_trip() -> None:
    payload = VisitCompletedPayload.model_validate(
        {
            **VISIT_COMPLETED_SAMPLE,
            "patient_name_encrypted": "gAAAA-opaque-fernet-token",
            "language": "or",
        }
    )
    wire = build_envelope(
        event_type=EventType.VISIT_COMPLETED,
        payload=payload,
        source=EventSource.QUEUECARE,
        tenant_id="t-1",
    ).model_dump(mode="json")
    assert wire["data"]["patient_name_encrypted"] == "gAAAA-opaque-fernet-token"
    assert wire["data"]["language"] == "or"
    assert parse_payload(parse_envelope(wire)) == payload


def test_visit_completed_without_new_fields_is_identical_to_v021() -> None:
    payload = VisitCompletedPayload.model_validate(VISIT_COMPLETED_SAMPLE)
    assert payload.patient_name_encrypted is None
    assert payload.language is None
    wire = build_envelope(
        event_type=EventType.VISIT_COMPLETED,
        payload=payload,
        source=EventSource.QUEUECARE,
        tenant_id="t-1",
    ).model_dump(mode="json")
    # No null keys on the wire: a consumer on an older sm_common (extra="forbid")
    # still parses what a v0.22.0 producer sends without a name or language.
    assert wire["data"] == VISIT_COMPLETED_SAMPLE
    assert "patient_name_encrypted" not in json.loads(payload.model_dump_json())
    assert "language" not in json.loads(payload.model_dump_json())


def test_visit_completed_still_forbids_a_plaintext_name() -> None:
    for bad_key in ("patient_name", "name", "patient_first_name"):
        with pytest.raises(ValidationError):
            VisitCompletedPayload.model_validate({**VISIT_COMPLETED_SAMPLE, bad_key: "Animesh"})
