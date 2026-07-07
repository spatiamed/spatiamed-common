import httpx
import pytest
import respx
from httpx import Response

from sm_common.internal_http import INTERNAL_SECRET_HEADER, InternalClient

BASE_URL = "http://queuecare.internal"
SECRET = "s3cr3t-internal"


@respx.mock
@pytest.mark.asyncio
async def test_get_injects_secret_header():
    route = respx.get(f"{BASE_URL}/internal/foo").mock(return_value=Response(200, json={"ok": 1}))
    async with InternalClient(BASE_URL, SECRET) as client:
        resp = await client.get("/internal/foo")
    assert resp.status_code == 200
    assert route.calls[0].request.headers[INTERNAL_SECRET_HEADER] == SECRET


@respx.mock
@pytest.mark.asyncio
async def test_post_injects_secret_and_sends_json():
    route = respx.post(f"{BASE_URL}/internal/bar").mock(return_value=Response(201))
    async with InternalClient(BASE_URL, SECRET) as client:
        resp = await client.post("/internal/bar", json={"a": 1})
    assert resp.status_code == 201
    req = route.calls[0].request
    assert req.headers[INTERNAL_SECRET_HEADER] == SECRET
    assert req.content == b'{"a":1}'


@respx.mock
@pytest.mark.asyncio
async def test_post_content_sends_exact_bytes():
    route = respx.post(f"{BASE_URL}/internal/raw").mock(return_value=Response(200))
    body = b'{"signed":"bytes"}'
    async with InternalClient(BASE_URL, SECRET) as client:
        await client.post("/internal/raw", content=body)
    assert route.calls[0].request.content == body


@respx.mock
@pytest.mark.asyncio
async def test_injected_secret_overrides_caller_header():
    route = respx.get(f"{BASE_URL}/internal/foo").mock(return_value=Response(200))
    async with InternalClient(BASE_URL, SECRET) as client:
        # A caller trying to override the secret must NOT win.
        await client.get("/internal/foo", headers={INTERNAL_SECRET_HEADER: "attacker"})
    assert route.calls[0].request.headers[INTERNAL_SECRET_HEADER] == SECRET


@respx.mock
@pytest.mark.asyncio
async def test_caller_headers_are_merged():
    route = respx.get(f"{BASE_URL}/internal/foo").mock(return_value=Response(200))
    async with InternalClient(BASE_URL, SECRET) as client:
        await client.get("/internal/foo", headers={"X-Tenant-ID": "abc"})
    req = route.calls[0].request
    assert req.headers["X-Tenant-ID"] == "abc"
    assert req.headers[INTERNAL_SECRET_HEADER] == SECRET


@respx.mock
@pytest.mark.asyncio
async def test_request_low_level_method():
    route = respx.route(method="DELETE", url=f"{BASE_URL}/internal/x").mock(
        return_value=Response(204)
    )
    async with InternalClient(BASE_URL, SECRET) as client:
        resp = await client.request("DELETE", "/internal/x")
    assert resp.status_code == 204
    assert route.calls[0].request.headers[INTERNAL_SECRET_HEADER] == SECRET


@respx.mock
@pytest.mark.asyncio
async def test_persistent_client_mode():
    respx.get(f"{BASE_URL}/internal/a").mock(return_value=Response(200))
    respx.get(f"{BASE_URL}/internal/b").mock(return_value=Response(200))
    client = InternalClient(BASE_URL, SECRET)
    r1 = await client.get("/internal/a")
    r2 = await client.get("/internal/b")
    assert r1.status_code == 200
    assert r2.status_code == 200
    await client.aclose()


@pytest.mark.asyncio
async def test_custom_timeout_is_applied():
    client = InternalClient(BASE_URL, SECRET, timeout=2.0)
    assert client._client.timeout == httpx.Timeout(2.0)
    await client.aclose()
