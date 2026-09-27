"""SP3 §8.1–§8.2: a search that fails must never look like "no such patient"."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.exceptions import SearchNotSupported, TransientError

pytestmark = pytest.mark.asyncio


def _adapter(handler) -> FhirR4Adapter:
    a = FhirR4Adapter(base_url="https://hms.example/fhir", auth_scheme="bearer", auth_cfg={"bearer_token": "t"})
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def _bundle(*resources):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}


def _pat(pid="p1", name_text="Asha Rao", birth_date="1985-03-12"):
    return {"resourceType": "Patient", "id": pid, "name": [{"text": name_text}], "birthDate": birth_date}


async def test_search_5xx_raises_transient():
    a = _adapter(lambda r: httpx.Response(503, text="down"))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_search_transport_error_raises_transient():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientError):
        await _adapter(handler).search_patients(phone="9876543210")


async def test_search_non_json_body_is_transient():
    a = _adapter(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_search_raises_transient_when_the_token_fetch_fails():
    a = FhirR4Adapter(
        base_url="https://hms.example/fhir",
        auth_scheme="oauth2_client_credentials",
        auth_cfg={"token_url": "https://hms.example/token", "client_id": "c", "client_secret": "s"},
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_family_and_birthdate_query_shape():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=_bundle(_pat()))

    found = await _adapter(handler).search_patients(family="Rao", birth_date=date(1985, 3, 12))
    assert seen == {"family": "Rao", "birthdate": "eq1985-03-12"}
    assert [p.resource_id for p in found] == ["p1"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"family": "Rao"},
        {"birth_date": date(1985, 3, 12)},
        {"family": "Rao", "birth_date": date(1985, 3, 12), "phone": "9876543210"},
    ],
)
async def test_name_without_dob_or_mixed_search_is_refused(kwargs):
    def handler(request):
        raise AssertionError("must not call the vendor")

    with pytest.raises(ValueError):
        await _adapter(handler).search_patients(**kwargs)


async def test_empty_bundle_is_still_an_empty_list():
    a = _adapter(lambda r: httpx.Response(200, json=_bundle()))
    assert await a.search_patients(phone="9876543210") == []


async def test_get_patient_5xx_raises_transient():
    with pytest.raises(TransientError):
        await _adapter(lambda r: httpx.Response(502)).get_patient("p1")


async def test_get_patient_transport_error_raises_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientError):
        await _adapter(handler).get_patient("p1")


async def test_get_patient_404_is_still_none():
    assert await _adapter(lambda r: httpx.Response(404)).get_patient("gone") is None


async def test_find_patient_inherits_the_loud_failure():
    with pytest.raises(TransientError):
        await _adapter(lambda r: httpx.Response(500)).find_patient(mrn="x")


async def test_base_default_refuses_name_dob_search():
    adapter = GenericRestAdapter({"base_url": "https://x", "list_appointments_path": "/a"})
    with pytest.raises(SearchNotSupported):
        await adapter.search_patients(family="Rao", birth_date=date(1985, 3, 12))


async def test_family_prefix_match_results_are_filtered_client_side():
    """FINDINGS §14 P4: OpenEMR's family= is a case-insensitive starts-with match
    ("rao" also returns "Raorane"), so the adapter must filter to an exact,
    case-insensitive surname match itself (preflight F8/F9's whole-token-run
    shape — a substring/split-membership test wrongly drops multi-word surnames).
    """
    bundle = _bundle(
        _pat(pid="p1", name_text="Asha Rao"),
        _pat(pid="p2", name_text="Asha Raorane"),
    )

    a = _adapter(lambda r: httpx.Response(200, json=bundle))
    found = await a.search_patients(family="Rao", birth_date=date(1985, 3, 12))
    assert [p.resource_id for p in found] == ["p1"]


async def test_family_prefix_match_filter_keeps_multi_word_surnames():
    """A multi-word family ("Van Der X") must survive the exact filter: it is a
    contiguous run of whole tokens in name_token, not a single split-membership
    test (preflight F9 — that shape drops every multi-word surname)."""
    bundle = _bundle(
        _pat(pid="p1", name_text="Asha Van Der X"),
        _pat(pid="p2", name_text="Asha X"),
    )

    a = _adapter(lambda r: httpx.Response(200, json=bundle))
    found = await a.search_patients(family="Van Der X", birth_date=date(1985, 3, 12))
    assert [p.resource_id for p in found] == ["p1"]
