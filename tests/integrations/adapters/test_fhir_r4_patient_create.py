"""SP3 §5.2 / §8.4: FHIR Patient create, idempotent on our marker identifier."""

from __future__ import annotations

import json
from datetime import date
from uuid import UUID

import httpx
import pytest

from sm_common.integrations import PatientCreate
from sm_common.integrations.adapters.fhir_r4 import PATIENT_IDENTIFIER_SYSTEM, FhirR4Adapter
from sm_common.integrations.exceptions import (
    AuthError, ConflictError, TransientError, VendorRejected, WriteNotSupported,
)

pytestmark = pytest.mark.asyncio
MARKER = UUID("00000000-0000-0000-0000-0000000000a1")
TOKEN = f"{PATIENT_IDENTIFIER_SYSTEM}|{MARKER}"


def _adapter(handler) -> FhirR4Adapter:
    a = FhirR4Adapter(base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"})
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _pc(phone="9876543210"):
    return PatientCreate(patient_marker=MARKER, family="Rao", given=["Asha", "Devi"],
                         birth_date=date(1985, 3, 12), gender="F", phone=phone)


def _bundle(*resources):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}


def _created(id_="p-new", mrn="MRN-7"):
    return {"resourceType": "Patient", "id": id_,
            "identifier": [{"system": PATIENT_IDENTIFIER_SYSTEM, "value": str(MARKER)}, {"value": mrn}]}


async def test_create_carries_marker_if_none_exist_and_name_text():
    seen = {}

    def handler(request):
        if request.method == "GET":
            assert request.url.params["identifier"] == TOKEN
            return httpx.Response(200, json=_bundle())
        seen["body"] = json.loads(request.content)
        seen["ine"] = request.headers.get("If-None-Exist")
        return httpx.Response(201, json=_created())

    r = await _adapter(handler).create_patient(_pc())
    body = seen["body"]
    assert body["resourceType"] == "Patient"
    assert body["name"] == [{"use": "official", "text": "Asha Devi Rao", "family": "Rao", "given": ["Asha", "Devi"]}]
    assert body["birthDate"] == "1985-03-12" and body["gender"] == "female"
    # FINDINGS §14 P5: OpenEMR silently drops a telecom phone with no `use`, so
    # this must always carry use="mobile" — else Task 6's phone fallback and
    # Task 7's read-back check have nothing to find.
    assert body["telecom"] == [{"system": "phone", "use": "mobile", "value": "+919876543210"}]
    assert body["identifier"] == [{"system": PATIENT_IDENTIFIER_SYSTEM, "value": str(MARKER)}]
    assert seen["ine"] == f"identifier={TOKEN}"
    assert (r.resource_id, r.mrn, r.created) == ("p-new", "MRN-7", True)


async def test_phone_less_patient_has_no_telecom():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_created())

    await _adapter(handler).create_patient(_pc(phone=None))
    assert "telecom" not in seen["body"]


async def test_a_prior_marker_hit_returns_created_false_without_posting():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("must not POST when our record already exists")
        return httpx.Response(200, json=_bundle(_created("p-old")))

    r = await _adapter(handler).create_patient(_pc())
    assert (r.resource_id, r.created) == ("p-old", False)


async def test_several_marker_hits_raise_conflict():
    def handler(request):
        return httpx.Response(200, json=_bundle(_created("a"), _created("b")))

    with pytest.raises(ConflictError) as ei:
        await _adapter(handler).create_patient(_pc())
    assert ei.value.landed is True


async def test_create_marker_search_4xx_still_posts_with_if_none_exist():
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(400, json={"resourceType": "OperationOutcome", "issue": [{"code": "not-supported"}]})
        posted.append(request.headers.get("If-None-Exist"))
        return httpx.Response(201, json=_created())

    r = await _adapter(handler).create_patient(_pc())
    assert posted == [f"identifier={TOKEN}"] and r.created is True


async def test_marker_search_5xx_is_transient_and_never_posts():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("no create without a completed pre-search")
        return httpx.Response(503)

    with pytest.raises(TransientError):
        await _adapter(handler).create_patient(_pc())


@pytest.mark.parametrize(
    ("status", "exc"),
    [(404, WriteNotSupported), (405, WriteNotSupported), (401, AuthError), (403, AuthError),
     (409, ConflictError), (429, TransientError), (500, TransientError), (422, VendorRejected)],
)
async def test_post_status_mapping(status, exc):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        return httpx.Response(status, json={"resourceType": "OperationOutcome", "issue": [{"code": "invalid"}]})

    with pytest.raises(exc) as ei:
        await _adapter(handler).create_patient(_pc())
    assert "Rao" not in str(ei.value) and "9876543210" not in str(ei.value)  # PHI-safe


async def test_post_transport_error_is_transient():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientError):
        await _adapter(handler).create_patient(_pc())


async def test_create_2xx_without_id_is_vendor_rejected_landed():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_bundle())
        return httpx.Response(201, json={"resourceType": "Patient"})

    with pytest.raises(VendorRejected) as ei:
        await _adapter(handler).create_patient(_pc())
    assert ei.value.landed is True


async def test_conditional_create_200_empty_body_rereads_by_marker():
    calls = {"get": 0}

    def handler(request):
        if request.method == "GET":
            calls["get"] += 1
            return httpx.Response(200, json=_bundle() if calls["get"] == 1 else _bundle(_created("p-old")))
        return httpx.Response(200, content=b"")

    r = await _adapter(handler).create_patient(_pc())
    assert (r.resource_id, r.created) == ("p-old", False)


async def test_create_raises_transient_when_the_token_fetch_fails():
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="oauth2_client_credentials",
        auth_cfg={"token_url": "https://hms.example/token", "client_id": "c", "client_secret": "s"},
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(TransientError):
        await a.create_patient(_pc())
