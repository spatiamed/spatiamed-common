import pytest
import httpx
from unittest.mock import AsyncMock, patch

from sm_common.truecaller import TruecallerClient, TruecallerConfig

DUMMY_REQUEST = httpx.Request("POST", "https://test.example.com")


@pytest.fixture
def config():
    return TruecallerConfig(partner_key="test-partner-key")


@pytest.fixture
def client(config):
    return TruecallerClient(config)


@pytest.mark.asyncio
async def test_register_numbers(client):
    mock_response = httpx.Response(200, json={"status": "registered"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await client.register_numbers(
            numbers=["+919876543210"],
            business_name="City General Hospital",
        )
    assert result["status"] == "registered"


@pytest.mark.asyncio
async def test_register_numbers_request_format(client):
    mock_response = httpx.Response(200, json={"status": "ok"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.register_numbers(
            numbers=["+919876543210", "+919876543211"],
            business_name="City General Hospital",
            category="Clinic",
            logo_url="https://example.com/logo.png",
        )
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer test-partner-key"
    body = call_kwargs.kwargs["json"]
    assert body["phoneNumbers"] == ["+919876543210", "+919876543211"]
    assert body["businessName"] == "City General Hospital"
    assert body["category"] == "Clinic"
    assert body["logoUrl"] == "https://example.com/logo.png"


@pytest.mark.asyncio
async def test_register_numbers_default_category(client):
    mock_response = httpx.Response(200, json={"status": "ok"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.register_numbers(
            numbers=["+919876543210"],
            business_name="Test Hospital",
        )
    body = mock_post.call_args.kwargs["json"]
    assert body["category"] == "Hospital"
    assert body["logoUrl"] is None


@pytest.mark.asyncio
async def test_register_numbers_raises_on_error(client):
    mock_response = httpx.Response(403, json={"error": "forbidden"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            await client.register_numbers(
                numbers=["+919876543210"],
                business_name="Test",
            )


@pytest.mark.asyncio
async def test_close(client):
    with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
        await client.close()
    mock_close.assert_called_once()
