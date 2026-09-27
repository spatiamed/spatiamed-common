from __future__ import annotations

from datetime import date
from uuid import UUID

import httpx
import pytest

from sm_common.integrations import PatientCreate
from sm_common.integrations.adapters.openemr import OpenEmrAdapter
from sm_common.integrations.exceptions import ConflictError

pytestmark = pytest.mark.asyncio
MARKER = UUID("00000000-0000-0000-0000-0000000000b2")
OE = {"timezone": "Asia/Kolkata", "pc_catid": "5", "pc_facility": "3", "pc_billing_location": "3",
      "write_user": {"token_url": "https://oe/t", "client_id": "c", "username": "u", "password": "p"}}


def _adapter(handler) -> OpenEmrAdapter:
    a = OpenEmrAdapter(base_url="https://oe/apis/default/fhir", auth_scheme="bearer",
                       auth_cfg={"bearer_token": "t"}, openemr=OE)
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _pat(pid, family="Rao", dob="1985-03-12", phone="9876543210"):
    return {"resourceType": "Patient", "id": pid, "name": [{"text": f"Asha {family}", "family": family}],
            "birthDate": dob, "telecom": [{"system": "phone", "value": phone}]}


def _bundle(*r):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": x} for x in r]}


def _pc(exclude=frozenset(), phone="9876543210"):
    return PatientCreate(patient_marker=MARKER, family="Rao", given=["Asha"], birth_date=date(1985, 3, 12),
                         gender="F", phone=phone, exclude_ids=frozenset(exclude))


async def test_fallback_ignores_excluded_ids_so_a_twin_is_not_already_created():
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(1)
            return httpx.Response(201, json={"resourceType": "Patient", "id": "twin-b"})
        return httpx.Response(200, json=_bundle(_pat("twin-a")))

    r = await _adapter(handler).create_patient(_pc(exclude={"twin-a"}))
    assert posted == [1] and (r.resource_id, r.created) == ("twin-b", True)


async def test_fallback_one_new_id_is_our_earlier_record():
    def handler(request):
        if request.method == "POST":
            raise AssertionError("must not create twice")
        return httpx.Response(200, json=_bundle(_pat("pre"), _pat("ours")))

    r = await _adapter(handler).create_patient(_pc(exclude={"pre"}))
    assert (r.resource_id, r.created) == ("ours", False)


async def test_fallback_several_new_ids_raise_conflict():
    def handler(request):
        return httpx.Response(200, json=_bundle(_pat("x"), _pat("y")))

    with pytest.raises(ConflictError):
        await _adapter(handler).create_patient(_pc())


async def test_fallback_requires_exact_family_and_birthdate():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"resourceType": "Patient", "id": "new"})
        return httpx.Response(200, json=_bundle(_pat("other-dob", dob="1985-03-13"), _pat("other-fam", family="Rai")))

    r = await _adapter(handler).create_patient(_pc())
    assert r.created is True


async def test_phone_less_fallback_searches_family_and_birthdate_only():
    seen = []

    def handler(request):
        if request.method == "GET":
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=_bundle())
        return httpx.Response(201, json={"resourceType": "Patient", "id": "n"})

    await _adapter(handler).create_patient(_pc(phone=None))
    assert seen == [{"family": "Rao", "birthdate": "eq1985-03-12"}]
