from unittest.mock import AsyncMock, patch

import httpx
import pytest

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
    mock_response = httpx.Response(
        200, json={"status": "submitted", "messageId": "abc123"}, request=DUMMY_REQUEST
    )
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
    with patch.object(
        client._client, "post", new_callable=AsyncMock, return_value=mock_response
    ) as mock_post:
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
    with patch.object(
        client._client, "post", new_callable=AsyncMock, return_value=mock_response
    ) as mock_post:
        await client.send_template(
            to="9876543210",
            template_id="t",
            params=[],
            source="s",
        )
    assert mock_post.call_args.kwargs["data"]["destination"] == "919876543210"


@pytest.mark.asyncio
async def test_send_session_message(client):
    mock_response = httpx.Response(200, json={"status": "submitted"}, request=DUMMY_REQUEST)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await client.send_session_message(
            to="9876543210",
            text="Hello!",
            source="91XXXXX",
        )
    assert result["status"] == "submitted"


@pytest.mark.asyncio
async def test_send_template_raises_on_error(client):
    mock_response = httpx.Response(401, json={"error": "unauthorized"}, request=DUMMY_REQUEST)
    with (
        patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.send_template(
            to="9876543210",
            template_id="t",
            params=[],
            source="s",
        )


@pytest.mark.asyncio
async def test_close(client):
    with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
        await client.close()
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_send_template_blocked_when_destination_not_allowed():
    config = GupshupConfig(
        api_key="test-key",
        app_name="spatiamed",
        allowed_destinations=frozenset({"+91 98765 43210"}),
    )
    client = GupshupClient(config)
    with patch.object(client._client, "post", new_callable=AsyncMock) as post:
        result = await client.send_template(
            to="9812345678",
            template_id="tmpl_123",
            params=[],
            source="9198765XXXXX",
        )
    post.assert_not_awaited()
    assert result["status"] == "blocked_by_allowlist"
    assert result["messageId"] == ""


@pytest.mark.asyncio
async def test_send_template_allowed_when_destination_on_list():
    config = GupshupConfig(
        api_key="test-key",
        app_name="spatiamed",
        allowed_destinations=frozenset({"+91 98765 43210"}),
    )
    client = GupshupClient(config)
    mock_response = httpx.Response(
        200, json={"status": "submitted", "messageId": "abc123"}, request=DUMMY_REQUEST
    )
    with patch.object(
        client._client, "post", new_callable=AsyncMock, return_value=mock_response
    ) as post:
        result = await client.send_template(
            to="9876543210",
            template_id="tmpl_123",
            params=[],
            source="9198765XXXXX",
        )
    post.assert_awaited_once()
    assert result["status"] == "submitted"


@pytest.mark.asyncio
async def test_send_session_message_blocked_when_destination_not_allowed():
    config = GupshupConfig(
        api_key="test-key",
        app_name="spatiamed",
        allowed_destinations=frozenset({"919876543210"}),
    )
    client = GupshupClient(config)
    with patch.object(client._client, "post", new_callable=AsyncMock) as post:
        result = await client.send_session_message(
            to="9812345678", text="hi", source="9198765XXXXX"
        )
    post.assert_not_awaited()
    assert result["status"] == "blocked_by_allowlist"


@pytest.mark.asyncio
async def test_empty_allowlist_blocks_everything():
    config = GupshupConfig(
        api_key="test-key", app_name="spatiamed", allowed_destinations=frozenset()
    )
    client = GupshupClient(config)
    with patch.object(client._client, "post", new_callable=AsyncMock) as post:
        result = await client.send_template(to="9876543210", template_id="t", params=[], source="s")
    post.assert_not_awaited()
    assert result["status"] == "blocked_by_allowlist"


@pytest.mark.asyncio
async def test_none_allowlist_is_unrestricted(client):
    mock_response = httpx.Response(
        200, json={"status": "submitted", "messageId": "abc123"}, request=DUMMY_REQUEST
    )
    with patch.object(
        client._client, "post", new_callable=AsyncMock, return_value=mock_response
    ) as post:
        result = await client.send_template(to="9999999999", template_id="t", params=[], source="s")
    post.assert_awaited_once()
    assert result["status"] == "submitted"
