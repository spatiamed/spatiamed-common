from __future__ import annotations

import httpx
import pytest
import respx

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter

pytestmark = pytest.mark.asyncio

BASE = "https://hms.example/fhir"


def _adapter() -> FhirR4Adapter:
    return FhirR4Adapter(
        base_url=BASE,
        auth_scheme="bearer",
        auth_cfg={"bearer_token": "t"},
        hash_salt="salt",
    )


def _bundle(*entries: dict) -> dict:  # type: ignore[type-arg]
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(entries),
        "entry": [{"resource": e} for e in entries],
    }


def _patient(pid: str, name: str, phone: str) -> dict:  # type: ignore[type-arg]
    return {
        "resourceType": "Patient",
        "id": pid,
        "name": [{"text": name}],
        "telecom": [{"system": "phone", "value": phone}],
        "identifier": [{"value": f"MRN{pid}"}],
    }


@respx.mock
async def test_a_real_phone_searches_the_telecom_parameter() -> None:
    route = respx.get(f"{BASE}/Patient").mock(
        return_value=httpx.Response(200, json=_bundle(_patient("1", "Asha Rao", "+919876543210")))
    )
    adapter = _adapter()
    found = await adapter.find_patient(phone="+91 98765 43210")
    await adapter.close()

    assert found is not None
    assert "telecom" in route.calls[0].request.url.params


@respx.mock
async def test_the_search_covers_every_stored_phone_shape() -> None:
    # FHIR telecom is a TOKEN search — exact match. A hospital storing
    # "+919876543210" is not found by a search for "9876543210", so we OR the
    # plausible shapes (comma is OR in FHIR search).
    route = respx.get(f"{BASE}/Patient").mock(
        return_value=httpx.Response(200, json=_bundle(_patient("1", "Asha Rao", "+919876543210")))
    )
    adapter = _adapter()
    await adapter.find_patient(phone="9876543210")
    await adapter.close()

    telecom = route.calls[0].request.url.params["telecom"]
    sent = set(telecom.split(","))
    assert "+919876543210" in sent
    assert "9876543210" in sent


@respx.mock
async def test_the_hashed_phone_is_still_not_searchable() -> None:
    # Unchanged: a hash matches nothing on any HMS, so do not ask at all.
    route = respx.get(f"{BASE}/Patient").mock(return_value=httpx.Response(200, json=_bundle()))
    adapter = _adapter()
    found = await adapter.find_patient(phone_hash="deadbeef")
    await adapter.close()
    assert found is None
    assert route.call_count == 0


@respx.mock
async def test_mrn_still_wins_over_phone() -> None:
    route = respx.get(f"{BASE}/Patient").mock(
        return_value=httpx.Response(200, json=_bundle(_patient("1", "Asha Rao", "+919876543210")))
    )
    adapter = _adapter()
    await adapter.find_patient(mrn="MRN1", phone="+919876543210")
    await adapter.close()
    params = route.calls[0].request.url.params
    assert params["identifier"] == "MRN1"
    assert "telecom" not in params


@respx.mock
async def test_no_hint_at_all_makes_no_request() -> None:
    route = respx.get(f"{BASE}/Patient").mock(return_value=httpx.Response(200, json=_bundle()))
    adapter = _adapter()
    assert await adapter.find_patient() is None
    await adapter.close()
    assert route.call_count == 0


@respx.mock
async def test_an_empty_result_is_no_match_not_an_error() -> None:
    respx.get(f"{BASE}/Patient").mock(return_value=httpx.Response(200, json=_bundle()))
    adapter = _adapter()
    assert await adapter.find_patient(phone="+919876543210") is None
    await adapter.close()


@respx.mock
async def test_an_unparseable_phone_is_not_sent_to_the_hms() -> None:
    route = respx.get(f"{BASE}/Patient").mock(return_value=httpx.Response(200, json=_bundle()))
    adapter = _adapter()
    assert await adapter.find_patient(phone="not-a-number") is None
    await adapter.close()
    assert route.call_count == 0


# Every adapter's phone path, exercised. Without these the ABC change compiles
# and the whole suite passes while a missing import waits as a NameError on a
# path no test walks — which is exactly what happened while writing this.
@respx.mock
async def test_bahmni_accepts_a_real_phone() -> None:
    from sm_common.integrations.adapters.bahmni import BahmniAdapter

    respx.route(host="bahmni.example").mock(return_value=httpx.Response(200, json={}))
    adapter = BahmniAdapter(
        base_url="https://bahmni.example",
        auth_scheme="api_key",
        hash_salt="s",
        transport_key="k",
        api_key="a",
    )
    await adapter.find_patient(phone="9876543210")


@respx.mock
async def test_mocdoc_sends_a_real_phone_not_the_hash() -> None:
    from sm_common.integrations.adapters.mocdoc import MocDocAdapter

    route = respx.get("https://mocdoc.example/api/v1/patients").mock(
        return_value=httpx.Response(200, json={"patients": []})
    )
    adapter = MocDocAdapter(
        base_url="https://mocdoc.example",
        api_key="a",
        api_secret="s",
        hash_salt="h",
        transport_key="k",
    )
    await adapter.find_patient(phone="9876543210", phone_hash="deadbeef")

    if route.call_count:
        sent = route.calls[0].request.url.params
        assert "deadbeef" not in str(sent), "the join-space hash must never reach a vendor"


@respx.mock
async def test_generic_rest_prefers_the_real_phone_over_the_hash() -> None:
    from sm_common.integrations.adapters.generic_rest import GenericRestAdapter

    route = respx.get("https://rest.example/patients").mock(
        return_value=httpx.Response(200, json={"patients": []})
    )
    adapter = GenericRestAdapter({"base_url": "https://rest.example"})
    await adapter.find_patient(phone="9876543210", phone_hash="deadbeef")

    sent = str(route.calls[0].request.url.params)
    assert "deadbeef" not in sent, "the join-space hash must never reach a vendor"
    assert "9876543210" in sent
