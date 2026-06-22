import httpx
import pytest

from sm_common.integrations.auth import build_auth_headers


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
