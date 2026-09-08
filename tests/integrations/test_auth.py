import httpx
import pytest

from sm_common.integrations.auth import build_auth_headers

import urllib.parse

import jwt


def pem_public(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.mark.asyncio
async def test_api_key_scheme_uses_custom_header():
    async with httpx.AsyncClient() as c:
        h = await build_auth_headers(c, "api_key", {"api_key": "K", "api_key_header": "X-Foo"})
    assert h["X-Foo"] == "K"
    assert h["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_bearer_scheme():
    async with httpx.AsyncClient() as c:
        h = await build_auth_headers(c, "bearer", {"bearer_token": "tok"})
    assert h["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_hmac_scheme_signs_date_and_body():
    async with httpx.AsyncClient() as c:
        h = await build_auth_headers(c, "hmac", {"api_key": "K", "api_secret": "S"}, body="{}")
    assert h["X-Signature"]
    assert "Date" in h


@pytest.mark.asyncio
async def test_oauth2_client_credentials_fetches_and_caches_token():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/token"
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as c:
        cfg = {
            "token_url": "https://hms.example/token",
            "client_id": "cid",
            "client_secret": "sec",
            "scopes": "system/Appointment.read",
        }
        h1 = await build_auth_headers(c, "oauth2_client_credentials", cfg)
        h2 = await build_auth_headers(c, "oauth2_client_credentials", cfg)
    assert h1["Authorization"] == "Bearer AT"
    assert h2["Authorization"] == "Bearer AT"
    assert calls["n"] == 1  # cached on the cfg dict


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class TestPrivateKeyJwt:
    """SMART Backend Services auth — the only way to get system scopes.

    Measured against a live OpenEMR 8.3.0: client_credentials with a form-posted
    client_secret is refused; an RS384-signed client assertion against a
    registered jwks_uri is accepted. FHIR/ABDM vendors follow the same standard,
    so this is not an OpenEMR quirk.
    """

    @pytest.mark.asyncio
    async def test_sends_a_signed_assertion_and_returns_the_bearer_token(self):
        pem = _rsa_pem()
        seen: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(dict(urllib.parse.parse_qsl(req.content.decode())))
            return httpx.Response(200, json={"access_token": "sys-token", "expires_in": 3600})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            headers = await build_auth_headers(
                c,
                "private_key_jwt",
                {
                    "token_url": "https://hms.example/oauth2/token",
                    "client_id": "client-abc",
                    "private_key_pem": pem,
                    "kid": "key-1",
                    "scopes": "system/Appointment.read",
                },
            )

        assert headers["Authorization"] == "Bearer sys-token"
        assert seen["grant_type"] == "client_credentials"
        assert (
            seen["client_assertion_type"]
            == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        assert "client_secret" not in seen, "a private_key_jwt client must not send a secret"

        claims = jwt.decode(
            seen["client_assertion"],
            pem_public(pem),
            algorithms=["RS384"],
            audience="https://hms.example/oauth2/token",
        )
        assert claims["iss"] == "client-abc"
        assert claims["sub"] == "client-abc"
        assert claims["exp"] > claims["iat"]
        header = jwt.get_unverified_header(seen["client_assertion"])
        assert header["alg"] == "RS384"
        assert header["kid"] == "key-1"

    @pytest.mark.asyncio
    async def test_each_assertion_carries_a_unique_jti(self):
        """Replay protection: servers reject a reused jti."""
        pem = _rsa_pem()
        jtis: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            jtis.append(
                jwt.decode(form["client_assertion"], options={"verify_signature": False})["jti"]
            )
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

        cfg = {
            "token_url": "https://hms.example/oauth2/token",
            "client_id": "c",
            "private_key_pem": pem,
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await build_auth_headers(c, "private_key_jwt", dict(cfg))
            await build_auth_headers(c, "private_key_jwt", dict(cfg))

        assert len(set(jtis)) == 2, "jti must be unique per assertion"
