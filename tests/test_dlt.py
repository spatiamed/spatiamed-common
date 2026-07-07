import httpx
import pytest
import respx
from httpx import Response

from sm_common.dlt import DLT_TEMPLATE_PATTERN, register_template


def test_pattern_matches_10_digit():
    assert DLT_TEMPLATE_PATTERN.match("1234567890") is not None


def test_pattern_matches_19_digit():
    assert DLT_TEMPLATE_PATTERN.match("1234567890123456789") is not None


def test_pattern_rejects_too_short():
    assert DLT_TEMPLATE_PATTERN.match("123456789") is None


def test_pattern_rejects_non_numeric():
    assert DLT_TEMPLATE_PATTERN.match("12345abc90") is None


@respx.mock
@pytest.mark.asyncio
async def test_register_template_returns_dlt_id():
    route = respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
        return_value=Response(
            200, json={"status": "success", "template": {"id": "1107160000000012345"}}
        )
    )
    dlt_id = await register_template(
        body="Your token {{1}} is ready at {{2}}.",
        template_name="token_ready",
        channel="sms",
        api_key="test-key",
        app_name="spatiamed",
    )
    assert dlt_id == "1107160000000012345"
    assert route.called
    assert route.calls[0].request.headers["apikey"] == "test-key"


@respx.mock
@pytest.mark.asyncio
async def test_register_template_sends_expected_form_fields():
    route = respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
        return_value=Response(
            200, json={"status": "success", "template": {"id": "1107160000000012345"}}
        )
    )
    await register_template(
        body="Hello {{1}}",
        template_name="greeting",
        channel="sms",
        api_key="k",
        app_name="spatiamed",
    )
    content = route.calls[0].request.content.decode()
    assert "elementName=greeting" in content
    assert "appName=spatiamed" in content
    assert "channel=sms" in content


@respx.mock
@pytest.mark.asyncio
async def test_register_template_custom_base_url():
    route = respx.post("https://example.test/v2/template").mock(
        return_value=Response(
            200, json={"status": "success", "template": {"id": "1234567890"}}
        )
    )
    dlt_id = await register_template(
        body="b",
        template_name="t",
        channel="sms",
        api_key="k",
        app_name="a",
        base_url="https://example.test/v2",
    )
    assert dlt_id == "1234567890"
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_register_template_raises_on_http_error():
    respx.post("https://api.gupshup.io/wa/api/v1/template").mock(return_value=Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await register_template(
            body="b", template_name="t", channel="sms", api_key="k", app_name="a"
        )


@respx.mock
@pytest.mark.asyncio
async def test_register_template_raises_on_missing_id():
    respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
        return_value=Response(200, json={"status": "success", "template": {}})
    )
    with pytest.raises(ValueError, match="missing id"):
        await register_template(
            body="b", template_name="t", channel="sms", api_key="k", app_name="a"
        )


@respx.mock
@pytest.mark.asyncio
async def test_register_template_raises_on_template_null():
    """{"template": null} must raise ValueError, not AttributeError."""
    respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
        return_value=Response(200, json={"status": "success", "template": None})
    )
    with pytest.raises(ValueError, match="missing id"):
        await register_template(
            body="b", template_name="t", channel="sms", api_key="k", app_name="a"
        )


@respx.mock
@pytest.mark.asyncio
async def test_register_template_raises_on_json_list():
    """A JSON list body (non-dict) must raise ValueError, not AttributeError."""
    respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
        return_value=Response(200, json=[])
    )
    with pytest.raises(ValueError, match="missing id"):
        await register_template(
            body="b", template_name="t", channel="sms", api_key="k", app_name="a"
        )


@respx.mock
@pytest.mark.asyncio
async def test_register_template_raises_on_null_id():
    """{"template": {"id": null}} must raise ValueError, not return the string "None"."""
    respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
        return_value=Response(200, json={"status": "success", "template": {"id": None}})
    )
    with pytest.raises(ValueError, match="missing id"):
        await register_template(
            body="b", template_name="t", channel="sms", api_key="k", app_name="a"
        )
