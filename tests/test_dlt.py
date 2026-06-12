import httpx
import pytest
import respx
from httpx import Response

from sm_common.dlt import register_template, validate_dlt_template_id


def test_valid_10_digit():
    assert validate_dlt_template_id("1234567890") is True


def test_valid_19_digit():
    assert validate_dlt_template_id("1234567890123456789") is True


def test_valid_15_digit():
    assert validate_dlt_template_id("123456789012345") is True


def test_invalid_too_short():
    assert validate_dlt_template_id("123456789") is False


def test_invalid_too_long():
    assert validate_dlt_template_id("12345678901234567890") is False


def test_invalid_non_numeric():
    assert validate_dlt_template_id("12345abc90") is False


def test_invalid_empty():
    assert validate_dlt_template_id("") is False


def test_invalid_spaces():
    assert validate_dlt_template_id("123 456 7890") is False


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
    respx.post("https://api.gupshup.io/wa/api/v1/template").mock(
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
    content = respx.calls[0].request.content.decode()
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
