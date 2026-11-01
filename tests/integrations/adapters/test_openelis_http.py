"""Lab-system (OpenELIS) HTTP client: login, query providers, the collect call.

All values are synthetic; the host is a reserved ``.test`` name.
"""

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from sm_common.integrations.adapters.openelis import (
    OpenElisClient,
    OpenElisConfig,
    SampleTests,
)
from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    NotFound,
    TransientError,
    VendorRejected,
)

BASE = "https://lab.example.test"
LOGIN = f"{BASE}/openelis/ValidateLogin.do"
AJAX = f"{BASE}/openelis/ajaxQueryXML"
GUARD = "guard-key-test"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "openelis"


def _cfg(**over: object) -> OpenElisConfig:
    base: dict[str, object] = {
        "base_url": BASE,
        "username": "lab-user",
        "password": "lab-pw",
        "guard_header_value": GUARD,
        "ro_dsn": "",
        "accession_prefix": "SM",
    }
    base.update(over)
    return OpenElisConfig(**base)  # type: ignore[arg-type]


def _client(**over: object) -> OpenElisClient:
    return OpenElisClient(_cfg(**over))


def _login_ok(cookie: str = "s1") -> httpx.Response:
    return httpx.Response(
        200,
        text="<html>dashboard</html>",
        headers={"set-cookie": f"JSESSIONID={cookie}; Path=/openelis; HttpOnly"},
    )


def _xml() -> str:
    return (FIXTURES / "sample_type_tests.xml").read_text()


def _qs(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


# ─── config ──────────────────────────────────────────────────────────────────


def test_prefix_validation_refuses_digits():
    for bad in ("S1", "12", "", "sm", "ABCD", "S-", "SM\n"):
        with pytest.raises(ConfigError):
            _cfg(accession_prefix=bad)
    assert _cfg(accession_prefix="LAB").accession_prefix == "LAB"


def test_config_requires_guard_and_credentials():
    for key in ("base_url", "username", "password", "guard_header_value"):
        with pytest.raises(ConfigError):
            _cfg(**{key: ""})


def test_from_mapping_reads_secrets_from_creds():
    cfg = OpenElisConfig.from_mapping(
        {"base_url": BASE, "accession_prefix": "SM", "verify_tls": True},
        {
            "username": "lab-user",
            "password": "lab-pw",
            "guard_header_value": GUARD,
            "ro_dsn": "postgresql://sm_elis_ro:pw@lab-db.example.test:5432/clinlims",
        },
    )
    assert cfg.username == "lab-user"
    assert cfg.guard_header_value == GUARD
    assert cfg.ro_dsn.startswith("postgresql://")
    with pytest.raises(ConfigError):
        OpenElisConfig.from_mapping({"base_url": BASE, "accession_prefix": "SM"}, {})


# ─── login + guard header ────────────────────────────────────────────────────


@respx.mock
async def test_login_sets_session_and_guard_header():
    login = respx.post(LOGIN).mock(return_value=_login_ok("s1"))
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(200, text=_xml()))
    client = _client()
    await client.sample_tests(101)
    await client.sample_tests(101)

    assert login.call_count == 1
    form = parse_qs(login.calls[0].request.content.decode())
    assert form == {"loginName": ["lab-user"], "password": ["lab-pw"]}
    assert login.calls[0].request.headers["X-SM-Lab-Key"] == GUARD
    assert ajax.call_count == 2
    for call in ajax.calls:
        assert call.request.headers["X-SM-Lab-Key"] == GUARD
        assert "JSESSIONID=s1" in call.request.headers["Cookie"]
    await client.aclose()


@respx.mock
async def test_login_rejected_is_auth_error():
    respx.post(LOGIN).mock(return_value=httpx.Response(200, text="<html>login</html>"))
    client = _client()
    with pytest.raises(AuthError):
        await client.sample_tests(101)


@respx.mock
async def test_relogin_once_on_redirect_to_login():
    login = respx.post(LOGIN).mock(side_effect=[_login_ok("s1"), _login_ok("s2")])
    ajax = respx.get(AJAX).mock(
        side_effect=[
            httpx.Response(302, headers={"location": f"{BASE}/openelis/LoginPage.do"}),
            httpx.Response(200, text=_xml()),
        ]
    )
    client = _client()
    tests = await client.sample_tests(101)
    assert tests.type_ids == (4, 9)
    assert login.call_count == 2
    assert "JSESSIONID=s2" in ajax.calls[1].request.headers["Cookie"]


@respx.mock
async def test_second_401_after_relogin_is_auth_error():
    login = respx.post(LOGIN).mock(side_effect=[_login_ok("s1"), _login_ok("s2")])
    respx.get(AJAX).mock(return_value=httpx.Response(401))
    client = _client()
    with pytest.raises(AuthError):
        await client.sample_tests(101)
    assert login.call_count == 2


@respx.mock
async def test_read_error_mapping():
    respx.post(LOGIN).mock(return_value=_login_ok())
    route = respx.get(AJAX)
    client = _client()
    for status, exc in (
        (403, AuthError),
        (500, TransientError),
        (404, NotFound),
        (400, VendorRejected),
    ):
        route.mock(return_value=httpx.Response(status))
        with pytest.raises(exc):
            await client.sample_tests(101)
    route.mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(TransientError):
        await client.sample_tests(101)


# ─── sample tests read ───────────────────────────────────────────────────────


@respx.mock
async def test_sample_tests_parsed():
    respx.post(LOGIN).mock(return_value=_login_ok())
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(200, text=_xml()))
    client = _client()
    tests = await client.sample_tests(101)
    assert tests == SampleTests(
        sample_id=101, type_ids=(4, 9), test_ids_by_type={4: (12, 13), 9: (31,)}
    )
    assert _qs(ajax.calls[0].request) == {
        "provider": "SampleTypeTestsForSampleProvider",
        "sampleId": "101",
    }


@respx.mock
async def test_sample_tests_rejects_duplicate_type():
    respx.post(LOGIN).mock(return_value=_login_ok())
    body = (
        "<fieldmessage><formfield><samples>"
        "<sample><sampleType>4</sampleType><test>12</test></sample>"
        "<sample><sampleType>4</sampleType><test>13</test></sample>"
        "</samples></formfield><message>valid</message></fieldmessage>"
    )
    respx.get(AJAX).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(VendorRejected):
        await _client().sample_tests(101)


@respx.mock
async def test_xml_parser_is_defused():
    respx.post(LOGIN).mock(return_value=_login_ok())
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE fieldmessage ['
        '<!ENTITY a "12"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        "<fieldmessage><formfield><samples><sample><sampleType>4</sampleType>"
        "<test>&b;</test></sample></samples></formfield><message>valid</message></fieldmessage>"
    )
    respx.get(AJAX).mock(return_value=httpx.Response(200, text=bomb))
    with pytest.raises(VendorRejected):
        await _client().sample_tests(101)


@respx.mock
async def test_unknown_provider_refused_before_io():
    login = respx.post(LOGIN).mock(return_value=_login_ok())
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(200, text=_xml()))
    client = _client()
    with pytest.raises(ConfigError):
        await client._http.provider_get("SomeOtherWriteProvider", {"sampleId": "1"})
    assert login.call_count == 0
    assert ajax.call_count == 0


# ─── collect call ────────────────────────────────────────────────────────────

_TESTS = SampleTests(sample_id=101, type_ids=(4, 9), test_ids_by_type={4: (12, 13), 9: (31,)})


@respx.mock
async def test_collect_sends_exact_list_and_date_format():
    respx.post(LOGIN).mock(return_value=_login_ok())
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(204))
    client = _client()
    await client._send_collect(101, "SM2610250001", date(2026, 10, 5), _TESTS)

    assert ajax.call_count == 1
    req = ajax.calls[0].request
    assert req.method == "GET"
    assert req.headers["X-SM-Lab-Key"] == GUARD
    qs = _qs(req)
    assert qs["provider"] == "TestUpdateWithAccessionNumberProvider"
    assert qs["sampleId"] == "101"
    assert qs["accessionNumber"] == "SM2610250001"
    assert qs["collectionDate"] == "05/10/2026"
    assert json.loads(qs["typeAndTestIds"]) == {
        "tests": {"0": "12,13", "1": "31"},
        "types": {"0": "4", "1": "9"},
    }


@respx.mock
async def test_non_204_is_rejected():
    respx.post(LOGIN).mock(return_value=_login_ok())
    route = respx.get(AJAX)
    client = _client()
    # 200 with an empty body is what the lab system returns for a malformed list.
    for resp in (
        httpx.Response(200, text=""),
        httpx.Response(200, text=_xml()),
        httpx.Response(500),
        httpx.Response(404),
        httpx.Response(302, headers={"location": f"{BASE}/openelis/LoginPage.do"}),
    ):
        route.mock(return_value=resp)
        with pytest.raises(VendorRejected):
            await client._send_collect(101, "SM2610250001", date(2026, 10, 5), _TESTS)
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(VendorRejected):
        await client._send_collect(101, "SM2610250001", date(2026, 10, 5), _TESTS)


@respx.mock
async def test_collect_never_resent():
    login = respx.post(LOGIN).mock(side_effect=[_login_ok("s1"), _login_ok("s2")])
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(401))
    client = _client()
    with pytest.raises(AuthError):
        await client._send_collect(101, "SM2610250001", date(2026, 10, 5), _TESTS)
    assert ajax.call_count == 1
    assert login.call_count == 1


@pytest.mark.parametrize(
    ("sample_id", "accession", "tests"),
    [
        # a type with no tests would send "" (or omit the type and cancel its item)
        (101, "SM2610250001", SampleTests(101, (4, 9), {4: (12,), 9: ()})),
        # no types at all would cancel every sample item
        (101, "SM2610250001", SampleTests(101, (), {})),
        # tests read for another sample
        (101, "SM2610250001", SampleTests(102, (4,), {4: (12,)})),
        # type list and test map disagree
        (101, "SM2610250001", SampleTests(101, (4,), {4: (12,), 9: (31,)})),
        # the lab system's own date-number format
        (101, "25102026-001", SampleTests(101, (4,), {4: (12,)})),
        # longer than 13 characters
        (101, "SM261025000123", SampleTests(101, (4,), {4: (12,)})),
        # not our prefix
        (101, "XY2610250001", SampleTests(101, (4,), {4: (12,)})),
        # a trailing newline would be stored in the accession as %0A
        (101, "SM2610250001\n", SampleTests(101, (4,), {4: (12,)})),
    ],
)
@respx.mock
async def test_collect_refuses_unsafe_input_before_io(sample_id, accession, tests):
    login = respx.post(LOGIN).mock(return_value=_login_ok())
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(204))
    with pytest.raises(VendorRejected):
        await _client()._send_collect(sample_id, accession, date(2026, 10, 5), tests)
    assert ajax.call_count == 0
    assert login.call_count == 0


# ─── fix round 1 ─────────────────────────────────────────────────────────────


@respx.mock
async def test_login_form_with_session_cookie_is_auth_error():
    # A failed login is forwarded back to the login form and still gets a JSESSIONID.
    respx.post(LOGIN).mock(
        return_value=httpx.Response(
            200,
            text='<html><form><input name="loginName"/></form></html>',
            headers={"set-cookie": "JSESSIONID=s1; Path=/openelis; HttpOnly"},
        )
    )
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(200, text=_xml()))
    with pytest.raises(AuthError):
        await _client().sample_tests(101)
    assert ajax.call_count == 0


@respx.mock
async def test_login_redirect_to_login_page_is_auth_error():
    respx.post(LOGIN).mock(
        return_value=httpx.Response(
            302,
            headers={
                "location": f"{BASE}/openelis/LoginPage.do",
                "set-cookie": "JSESSIONID=s1; Path=/openelis; HttpOnly",
            },
        )
    )
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(200, text=_xml()))
    with pytest.raises(AuthError):
        await _client().sample_tests(101)
    assert ajax.call_count == 0


@respx.mock
async def test_read_params_cannot_override_provider():
    login = respx.post(LOGIN).mock(return_value=_login_ok())
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(200, text=_xml()))
    client = _client()
    with pytest.raises(ConfigError):
        await client._http.provider_get(
            "SampleTypeTestsForSampleProvider",
            {"provider": "TestUpdateWithAccessionNumberProvider", "sampleId": "1"},
        )
    assert login.call_count == 0
    assert ajax.call_count == 0


@respx.mock
async def test_write_params_cannot_override_provider():
    login = respx.post(LOGIN).mock(return_value=_login_ok())
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(204))
    client = _client()
    with pytest.raises(ConfigError):
        await client._http.provider_send_once(
            "TestUpdateWithAccessionNumberProvider",
            {"provider": "SomeOtherWriteProvider", "sampleId": "1"},
        )
    assert login.call_count == 0
    assert ajax.call_count == 0


def test_insecure_tls_needs_explicit_opt_in():
    with pytest.raises(ConfigError):
        _cfg(verify_tls=False)
    assert _cfg(verify_tls=False, allow_insecure_tls=True).verify_tls is False
    with pytest.raises(ConfigError):
        OpenElisConfig.from_mapping(
            {"base_url": BASE, "accession_prefix": "SM", "verify_tls": False},
            {"username": "u", "password": "p", "guard_header_value": GUARD},
        )
    cfg = OpenElisConfig.from_mapping(
        {
            "base_url": BASE,
            "accession_prefix": "SM",
            "verify_tls": False,
            "allow_insecure_tls": True,
        },
        {"username": "u", "password": "p", "guard_header_value": GUARD},
    )
    assert cfg.allow_insecure_tls is True
    # A pinned CA bundle verifies TLS, so no opt-in is needed.
    _cfg(verify_tls=False, ca_bundle_pem="-----BEGIN CERTIFICATE-----")


def test_http_insecure_tls_needs_explicit_opt_in():
    from sm_common.integrations.adapters.openelis import OpenElisHttp

    with pytest.raises(ConfigError):
        OpenElisHttp(BASE, "u", "p", GUARD, verify_tls=False)


@respx.mock
async def test_collect_login_transport_failure_is_not_unconfirmed():
    # Nothing was sent, so the error is a plain transient one, not "outcome unknown".
    respx.post(LOGIN).mock(side_effect=httpx.ConnectTimeout("slow"))
    ajax = respx.get(AJAX).mock(return_value=httpx.Response(204))
    with pytest.raises(TransientError):
        await _client()._send_collect(101, "SM2610250001", date(2026, 10, 5), _TESTS)
    assert ajax.call_count == 0
