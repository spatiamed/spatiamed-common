"""v0.28.0 (Marketing Team OS): optional marketing fields on four existing payloads.

The fields are optional and dropped from the wire when None, so a payload
without them is byte-identical to v0.27.0 and a consumer still pinned to an
older ``sm_common`` (``extra="forbid"``) parses it. The baseline fixture
``fixtures/events_v027_marketing_baseline.json`` was generated from the
untouched v0.27.0 models and must never be regenerated from this version.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from sm_common.events import (
    ENQUIRY_REF_PATTERN,
    REFERRAL_CODE_PATTERN,
    BookingUpdatedPayload,
    VisitCompletedPayload,
    VisitNoShowPayload,
    VisitTokenIssuedPayload,
)

_BASELINE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "events_v027_marketing_baseline.json").read_text()
)

_MODELS: dict[str, type[BaseModel]] = {
    "booking.updated": BookingUpdatedPayload,
    "visit.completed": VisitCompletedPayload,
    "visit.no_show": VisitNoShowPayload,
    "visit.token_issued": VisitTokenIssuedPayload,
}


def test_referral_code_pattern_is_the_contract_literal() -> None:
    # CareLoop lane 02 and QueueCare copy this literal; both assert equality.
    assert REFERRAL_CODE_PATTERN == r"^[A-Z0-9][A-Z0-9-]{2,31}$"
    assert ENQUIRY_REF_PATTERN == r"^ENQ-[A-Z2-7]{6,8}$"


@pytest.mark.parametrize("event_type", sorted(_MODELS))
def test_without_marketing_fields_is_byte_identical_to_v027(event_type: str) -> None:
    baseline = _BASELINE[event_type]
    model = _MODELS[event_type].model_validate(baseline["payload"])
    assert model.model_dump_json() == baseline["json"]
    assert model.model_dump(mode="json") == baseline["payload"]


def test_booking_updated_without_marketing_fields_is_byte_identical_to_v027() -> None:
    baseline = _BASELINE["booking.updated"]
    p = BookingUpdatedPayload.model_validate(
        {**baseline["payload"], "referral_code": None, "enquiry_ref": None, "booking_source": None}
    )
    assert p.model_dump_json() == baseline["json"]


def test_booking_updated_carries_marketing_fields() -> None:
    p = BookingUpdatedPayload.model_validate(
        {
            **_BASELINE["booking.updated"]["payload"],
            "referral_code": "RF-SHETTY",
            "enquiry_ref": "ENQ-7F3A2Q",
            "booking_source": "careloop_campaign",
        }
    )
    d = p.model_dump(mode="json")
    assert d["referral_code"] == "RF-SHETTY"
    assert d["enquiry_ref"] == "ENQ-7F3A2Q"
    assert d["booking_source"] == "careloop_campaign"


def test_visit_completed_omits_none_booking_id_and_code() -> None:
    base = _BASELINE["visit.completed"]["payload"]
    d = VisitCompletedPayload.model_validate(
        {**base, "booking_id": None, "referral_code": None}
    ).model_dump(mode="json")
    assert "booking_id" not in d
    assert "referral_code" not in d
    d2 = VisitCompletedPayload.model_validate(
        {**base, "booking_id": "b-1", "referral_code": "RF-SHETTY"}
    ).model_dump(mode="json")
    assert d2["booking_id"] == "b-1"
    assert d2["referral_code"] == "RF-SHETTY"


def test_visit_no_show_omits_none_fields() -> None:
    base = _BASELINE["visit.no_show"]["payload"]
    d = VisitNoShowPayload.model_validate(base).model_dump(mode="json")
    assert "booking_id" not in d and "referral_code" not in d
    d2 = VisitNoShowPayload.model_validate(
        {**base, "booking_id": "b-2", "referral_code": "DR-MEHTA-01"}
    ).model_dump(mode="json")
    assert d2["booking_id"] == "b-2"
    assert d2["referral_code"] == "DR-MEHTA-01"


def test_token_issued_omits_none_code() -> None:
    base = _BASELINE["visit.token_issued"]["payload"]
    d = VisitTokenIssuedPayload.model_validate(base).model_dump(mode="json")
    assert "referral_code" not in d
    # pre_registration_id was emitted as null in v0.27 and still is (not in the omit set).
    assert "pre_registration_id" in d
    d2 = VisitTokenIssuedPayload.model_validate({**base, "referral_code": "CAMP-01"}).model_dump(
        mode="json"
    )
    assert d2["referral_code"] == "CAMP-01"


@pytest.mark.parametrize(
    "bad", ["rf-shetty", "RF SHETTY", "RF_SHETTY", "-RF", "AB", "A" * 33, " RF-1", "RF-1\n"]
)
@pytest.mark.parametrize("event_type", sorted(_MODELS))
def test_referral_code_pattern_rejects_lowercase_and_spaces(bad: str, event_type: str) -> None:
    with pytest.raises(ValidationError):
        _MODELS[event_type].model_validate(
            {**_BASELINE[event_type]["payload"], "referral_code": bad}
        )


@pytest.mark.parametrize("good", ["RF-SHETTY", "ABC", "A" * 32, "9X-1"])
def test_referral_code_pattern_accepts_valid(good: str) -> None:
    assert re.fullmatch(REFERRAL_CODE_PATTERN, good)
    VisitTokenIssuedPayload.model_validate(
        {**_BASELINE["visit.token_issued"]["payload"], "referral_code": good}
    )


@pytest.mark.parametrize(
    "bad", ["ENQ-7f3a2q", "ENQ-7F3A2", "ENQ-7F3A2Q01X", "ENQ-1F3A2Q", "QC-7F3A2Q"]
)
def test_enquiry_ref_pattern(bad: str) -> None:
    base = _BASELINE["booking.updated"]["payload"]
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate({**base, "enquiry_ref": bad})
    for good in ("ENQ-7F3A2Q", "ENQ-ABCDEFGH"):
        BookingUpdatedPayload.model_validate({**base, "enquiry_ref": good})


def test_booking_source_max_length() -> None:
    base = _BASELINE["booking.updated"]["payload"]
    BookingUpdatedPayload.model_validate({**base, "booking_source": "x" * 30})
    with pytest.raises(ValidationError):
        BookingUpdatedPayload.model_validate({**base, "booking_source": "x" * 31})


class _OldBookingUpdated(BaseModel):
    """A v0.27-shaped consumer copy: strict, no marketing fields."""

    model_config = ConfigDict(extra="forbid")
    booking_id: str
    phone_hash: str
    status: str
    slot_start: str | None
    doctor_name: str | None
    updated_at: str
    status_link: str | None = None
    lang: str | None = None
    contact_phone_encrypted: str | None = None
    visit_type: str | None = None
    notice_kind: str | None = None
    triage_call_enabled: bool | None = None
    triage_call_consent: bool | None = None


class _OldVisitNoShow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_hash: str
    token_number: str
    department_code: str = ""
    doctor_name: str = ""
    was_pre_registered: bool = False
    trigger_source: str = "manual"


def test_old_consumer_parses_new_producer_without_fields() -> None:
    new_bu = BookingUpdatedPayload.model_validate(
        {**_BASELINE["booking.updated"]["payload"], "referral_code": None}
    )
    _OldBookingUpdated.model_validate_json(new_bu.model_dump_json())
    new_ns = VisitNoShowPayload.model_validate(_BASELINE["visit.no_show"]["payload"])
    _OldVisitNoShow.model_validate_json(new_ns.model_dump_json())
    # ...and a strict old consumer DOES reject a payload that carries the new fields,
    # which is why producers only fill them where known.
    with pytest.raises(ValidationError):
        _OldVisitNoShow.model_validate(
            {**_BASELINE["visit.no_show"]["payload"], "referral_code": "RF-1X"}
        )


def test_token_issued_carries_optional_booking_id() -> None:
    """A token for a booking says which (CareLoop must not count the arrival of a
    referral booking as a second, walk-in booking). Omitted when None."""
    base = _BASELINE["visit.token_issued"]["payload"]
    d = VisitTokenIssuedPayload.model_validate({**base, "booking_id": None}).model_dump(mode="json")
    assert "booking_id" not in d
    d2 = VisitTokenIssuedPayload.model_validate(
        {**base, "booking_id": "b-1", "referral_code": "RF-SHETTY"}
    ).model_dump(mode="json")
    assert (d2["booking_id"], d2["referral_code"]) == ("b-1", "RF-SHETTY")
