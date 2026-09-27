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


def _pat_names(pid, names: list[dict], birth_date="1985-03-12"):
    return {"resourceType": "Patient", "id": pid, "name": names, "birthDate": birth_date}


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


# ─── fix3: filter must match structured name[*].family, not only the display
# token derived from name[0] — a real hit must never be dropped, because an
# empty result unlocks Create in HMS (duplicate chart). ──────────────────────


async def test_family_filter_matches_structured_family_despite_family_comma_given_text():
    """fix3-findings #1, case 1+2: text is "Family, Given" (or carries an
    honorific glued to the surname). name_token tokenises to ["rao,", "asha"],
    so a token-run match against name_token alone would drop this real hit.
    The structured `family` field must be checked instead."""
    bundle = _bundle(
        _pat_names("p1", [{"text": "Rao, Asha", "family": "Rao", "given": ["Asha"]}])
    )

    a = _adapter(lambda r: httpx.Response(200, json=bundle))
    found = await a.search_patients(family="Rao", birth_date=date(1985, 3, 12))
    assert [p.resource_id for p in found] == ["p1"]


async def test_family_filter_matches_structured_family_on_a_later_name_entry():
    """fix3-findings #1, case 3: the searched surname sits on name[1] (current
    name), not name[0] (an old/maiden name). The server matched it; the filter
    must not throw the hit away because name_token is derived from name[0]."""
    bundle = _bundle(
        _pat_names(
            "p1",
            [
                {"use": "old", "family": "Mehta", "given": ["Asha"]},
                {"family": "Rao", "given": ["Asha"]},
            ],
        )
    )

    a = _adapter(lambda r: httpx.Response(200, json=bundle))
    found = await a.search_patients(family="Rao", birth_date=date(1985, 3, 12))
    assert [p.resource_id for p in found] == ["p1"]


async def test_family_filter_still_rejects_a_structured_family_prefix_superset():
    """The structured-field match must stay exact (not a prefix match): a
    patient whose structured family is "Raorane" must not match a search for
    "Rao", the same guarantee test_family_prefix_match_results_are_filtered_client_side
    proves for the text-only fallback path."""
    bundle = _bundle(
        _pat_names("p1", [{"family": "Rao", "given": ["Asha"]}]),
        _pat_names("p2", [{"family": "Raorane", "given": ["Asha"]}]),
    )

    a = _adapter(lambda r: httpx.Response(200, json=bundle))
    found = await a.search_patients(family="Rao", birth_date=date(1985, 3, 12))
    assert [p.resource_id for p in found] == ["p1"]


async def test_whitespace_only_family_is_refused_not_silently_empty():
    """fix3-findings #2: a whitespace-only family passes `not family` (a
    non-empty string) and would send family="" and quietly return [] — an
    empty "nobody" answer belongs to ValueError, not to a silent search."""

    def handler(request):
        raise AssertionError("must not call the vendor")

    with pytest.raises(ValueError):
        await _adapter(handler).search_patients(family="   ", birth_date=date(1985, 3, 12))


async def test_get_patient_410_is_still_none():
    """fix3-findings #4: FHIR Gone is the same "nothing here" answer as 404."""
    assert await _adapter(lambda r: httpx.Response(410)).get_patient("gone-for-good") is None


async def test_search_valid_json_non_bundle_body_is_transient():
    """fix3-findings #4: a 200 with well-formed JSON that isn't a Bundle (e.g.
    an OperationOutcome) must not be read as an empty result set."""
    a = _adapter(lambda r: httpx.Response(200, json={"resourceType": "OperationOutcome"}))
    with pytest.raises(TransientError):
        await a.search_patients(phone="9876543210")


async def test_get_patient_non_json_body_is_transient():
    """fix3-findings #4: a 200 with a non-JSON body must not be read as absence."""
    with pytest.raises(TransientError):
        await _adapter(lambda r: httpx.Response(200, text="<html>proxy error</html>")).get_patient(
            "p1"
        )


async def test_search_skips_non_dict_bundle_entries():
    """fix3-findings #3: a malformed `entry` item (not a dict) must not crash
    the search with a raw AttributeError; the well-formed entries still return."""
    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": ["not-a-dict", {"resource": "also-not-a-dict"}, {"resource": _pat(pid="p1")}],
    }
    a = _adapter(lambda r: httpx.Response(200, json=bundle))
    found = await a.search_patients(phone="9876543210")
    assert [p.resource_id for p in found] == ["p1"]


async def test_get_patient_non_dict_json_body_is_transient():
    """fix3-findings #3: a 200 body that is valid JSON but not an object (e.g.
    a JSON list) must not crash with a raw AttributeError on `.get`."""
    a = _adapter(lambda r: httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(TransientError):
        await a.get_patient("p1")
