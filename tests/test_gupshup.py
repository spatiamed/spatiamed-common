import pytest
import httpx
from unittest.mock import AsyncMock, patch

from sm_common.gupshup import GupshupClient, GupshupConfig

DUMMY_REQUEST = httpx.Request("POST", "https://test.example.com")


@pytest.fixture
def config():
    return GupshupConfig(api_key="test-key", app_name="spatiamed")


@pytest.fixture
def client(config):
    return GupshupClient(config)


@pytest.mark.asyncio
async def test_send_template(client):
    mock_response = httpx.Response(200, json={"status": "submitted", "messageId": "abc123"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await client.send_template(
            to="9876543210",
            template_id="tmpl_123",
            params=["Rahul", "10:30 AM"],
            source="9198765XXXXX",
        )
    assert result["status"] == "submitted"


@pytest.mark.asyncio
async def test_send_template_custom_app_name(client):
    mock_response = httpx.Response(200, json={"status": "submitted"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.send_template(
            to="9876543210",
            template_id="tmpl_123",
            params=["val"],
            source="91XXXXX",
            app_name="custom-app",
        )
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["data"]["src.name"] == "custom-app"


@pytest.mark.asyncio
async def test_send_template_destination_prefix(client):
    mock_response = httpx.Response(200, json={"status": "ok"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.send_template(
            to="9876543210", template_id="t", params=[], source="s",
        )
    assert mock_post.call_args.kwargs["data"]["destination"] == "919876543210"


@pytest.mark.asyncio
async def test_send_session_message(client):
    mock_response = httpx.Response(200, json={"status": "submitted"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await client.send_session_message(
            to="9876543210", text="Hello!", source="91XXXXX",
        )
    assert result["status"] == "submitted"


@pytest.mark.asyncio
async def test_send_template_raises_on_error(client):
    mock_response = httpx.Response(401, json={"error": "unauthorized"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_template(
                to="9876543210", template_id="t", params=[], source="s",
            )


@pytest.mark.asyncio
async def test_close(client):
    with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
        await client.close()
    mock_close.assert_called_once()
