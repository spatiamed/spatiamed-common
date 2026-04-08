import pytest
import httpx
from unittest.mock import AsyncMock, patch

from sm_common.msg91 import MSG91Client, MSG91Config

DUMMY_REQUEST = httpx.Request("POST", "https://test.example.com")


@pytest.fixture
def config():
    return MSG91Config(auth_key="test-auth-key")


@pytest.fixture
def client(config):
    return MSG91Client(config)


@pytest.mark.asyncio
async def test_send_sms(client):
    mock_response = httpx.Response(200, json={"type": "success"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await client.send_sms(
            to="9876543210",
            template_id="1234567890123",
            params={"var1": "Rahul", "var2": "10:30 AM"},
            sender_id="CITYGH",
        )
    assert result["type"] == "success"


@pytest.mark.asyncio
async def test_send_sms_request_format(client):
    mock_response = httpx.Response(200, json={"type": "success"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.send_sms(
            to="9876543210",
            template_id="tmpl_123",
            params={"var1": "val"},
            sender_id="CITYGH",
        )
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["headers"]["authkey"] == "test-auth-key"
    body = call_kwargs.kwargs["json"]
    assert body["template_id"] == "tmpl_123"
    assert body["sender"] == "CITYGH"
    assert body["recipients"][0]["mobiles"] == "919876543210"
    assert body["recipients"][0]["var1"] == "val"


@pytest.mark.asyncio
async def test_send_sms_raises_on_error(client):
    mock_response = httpx.Response(401, json={"error": "unauthorized"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_sms(
                to="9876543210", template_id="t", params={}, sender_id="CITYGH",
            )


@pytest.mark.asyncio
async def test_close(client):
    with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
        await client.close()
    mock_close.assert_called_once()
