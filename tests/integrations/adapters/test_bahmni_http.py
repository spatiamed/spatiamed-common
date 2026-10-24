import datetime as dt
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from sm_common.integrations.adapters.bahmni_http import BahmniHttp, from_epoch_ms, to_epoch_ms
from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    TransientError,
    VendorRejected,
)

BASE = "https://bahmni.example.test"
SESSION = f"{BASE}/openmrs/ws/rest/v1/session"
PATH = "/openmrs/ws/rest/v1/patient"


def _login_ok(cookie: str = "abc") -> httpx.Response:
    return httpx.Response(
        200,
        json={"authenticated": True},
        headers={"set-cookie": f"JSESSIONID={cookie}; Path=/openmrs; HttpOnly"},
    )


def _http() -> BahmniHttp:
    return BahmniHttp(BASE, "user", "pw")


def test_epoch_roundtrip():
    assert from_epoch_ms(1792904400000) == datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    ist = datetime(2026, 10, 25, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert to_epoch_ms(ist) == 1792904400000
    assert from_epoch_ms("1792904400000") == from_epoch_ms(1792904400000.0)
    assert to_epoch_ms(datetime(2026, 10, 25, 5, 0, 0, 123000, tzinfo=timezone.utc)) == (
        1792904400123
    )


def test_to_epoch_ms_rejects_naive():
    with pytest.raises(ValueError):
        to_epoch_ms(datetime(2026, 10, 25, 5, 0))


def test_from_epoch_ms_rejects_garbage():
    with pytest.raises(ValueError):
        from_epoch_ms("not-a-number")
    with pytest.raises(ValueError):
        from_epoch_ms(10**18)  # beyond datetime range: overflow surfaces as ValueError


@respx.mock
async def test_login_uses_basic_and_caches_cookie():
    login = respx.get(SESSION).mock(return_value=_login_ok("abc"))
    data = respx.get(f"{BASE}{PATH}").mock(return_value=httpx.Response(200, json={"results": []}))
    http = _http()
    await http.request("GET", PATH)
    await http.request("GET", PATH)
    assert login.call_count == 1
    assert login.calls[0].request.headers["authorization"].startswith("Basic ")
    for call in data.calls:
        assert "JSESSIONID=abc" in call.request.headers["cookie"]
        assert call.request.headers["authorization"].startswith("Basic ")
    await http.aclose()


@respx.mock
async def test_login_authenticated_false_raises_auth():
    respx.get(SESSION).mock(return_value=httpx.Response(200, json={"authenticated": False}))
    with pytest.raises(AuthError):
        await _http().request("GET", PATH)


@respx.mock
async def test_login_401_raises_auth():
    respx.get(SESSION).mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await _http().request("GET", PATH)


@respx.mock
async def test_401_relogin_once():
    login = respx.get(SESSION).mock(side_effect=[_login_ok("one"), _login_ok("two")])
    data = respx.get(f"{BASE}{PATH}").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )
    http = _http()
    resp = await http.request("GET", PATH)
    assert resp.json() == {"ok": True}
    assert login.call_count == 2
    assert data.call_count == 2
    assert "JSESSIONID=two" in data.calls[1].request.headers["cookie"]


@respx.mock
async def test_second_401_raises_auth_without_third_attempt():
    login = respx.get(SESSION).mock(side_effect=[_login_ok("one"), _login_ok("two")])
    data = respx.get(f"{BASE}{PATH}").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await _http().request("GET", PATH)
    assert login.call_count == 2
    assert data.call_count == 2


@respx.mock
async def test_403_no_retry():
    login = respx.get(SESSION).mock(return_value=_login_ok())
    data = respx.get(f"{BASE}{PATH}").mock(return_value=httpx.Response(403))
    with pytest.raises(AuthError):
        await _http().request("GET", PATH)
    assert login.call_count == 1
    assert data.call_count == 1


@respx.mock
async def test_400_maps_to_vendor_rejected_with_message():
    respx.get(SESSION).mock(return_value=_login_ok())
    body = {"error": {"message": "No enum constant ... booked"}}
    respx.post(f"{BASE}{PATH}").mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(VendorRejected, match="No enum constant"):
        await _http().request("POST", PATH, json={"a": 1})


@respx.mock
async def test_400_non_json_body_truncated():
    respx.get(SESSION).mock(return_value=_login_ok())
    respx.post(f"{BASE}{PATH}").mock(return_value=httpx.Response(400, text="x" * 1000))
    with pytest.raises(VendorRejected) as ei:
        await _http().request("POST", PATH)
    assert "x" * 300 in str(ei.value)
    assert "x" * 301 not in str(ei.value)


@respx.mock
async def test_404_returned_not_raised():
    respx.get(SESSION).mock(return_value=_login_ok())
    respx.get(f"{BASE}{PATH}").mock(return_value=httpx.Response(404))
    resp = await _http().request("GET", PATH)
    assert resp.status_code == 404


@respx.mock
async def test_5xx_and_connect_error_transient():
    respx.get(SESSION).mock(return_value=_login_ok())
    route = respx.get(f"{BASE}{PATH}").mock(return_value=httpx.Response(503))
    http = _http()
    with pytest.raises(TransientError):
        await http.request("GET", PATH)
    route.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TransientError):
        await http.request("GET", PATH)
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TransientError):
        await http.request("GET", PATH)


@respx.mock
async def test_params_and_json_forwarded():
    respx.get(SESSION).mock(return_value=_login_ok())
    route = respx.post(f"{BASE}{PATH}").mock(return_value=httpx.Response(201, json={}))
    await _http().request("POST", PATH, params={"v": "full"}, json={"a": 1})
    req = route.calls[0].request
    assert req.url.params["v"] == "full"
    assert req.content == b'{"a":1}' or req.content == b'{"a": 1}'


def _self_signed_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bahmni.example.test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_ca_bundle_builds_ssl_context():
    http = BahmniHttp(BASE, "u", "p", ca_bundle_pem=_self_signed_pem())
    assert isinstance(http.verify, ssl.SSLContext)
    assert http.verify.verify_mode == ssl.CERT_REQUIRED


def test_default_verifies_tls():
    assert _http().verify is True


def test_insecure_requires_explicit_allow():
    with pytest.raises(ValueError, match="allow_insecure_tls"):
        BahmniHttp(BASE, "u", "p", verify_tls=False)
    assert BahmniHttp(BASE, "u", "p", verify_tls=False, allow_insecure_tls=True).verify is False


@respx.mock
async def test_bad_scheme_or_proxy_is_config_error_not_transient():
    respx.get(SESSION).mock(side_effect=httpx.UnsupportedProtocol("no scheme"))
    with pytest.raises(ConfigError):
        await _http().request("GET", PATH)
    respx.get(SESSION).mock(side_effect=httpx.ProxyError("bad proxy"))
    with pytest.raises(ConfigError):
        await _http().request("GET", PATH)


@respx.mock
async def test_remote_protocol_error_transient():
    respx.get(SESSION).mock(side_effect=httpx.RemoteProtocolError("reset"))
    with pytest.raises(TransientError):
        await _http().request("GET", PATH)
