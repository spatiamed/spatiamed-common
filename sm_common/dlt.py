import re

import httpx

DLT_TEMPLATE_PATTERN = re.compile(r"^\d{10,19}$")

_GUPSHUP_BASE_URL = "https://api.gupshup.io/wa/api/v1"


def validate_dlt_template_id(template_id: str) -> bool:
    """Validate that a template ID matches TRAI DLT format."""
    return bool(DLT_TEMPLATE_PATTERN.match(template_id))


async def register_template(
    *,
    body: str,
    template_name: str,
    channel: str,
    api_key: str,
    app_name: str,
    base_url: str = _GUPSHUP_BASE_URL,
) -> str:
    """Register a message template with the DLT registry via Gupshup.

    Returns the registered DLT template id. Raises httpx.HTTPStatusError on
    non-2xx and ValueError if the response lacks a template id.
    Registers with category=UTILITY and templateType=TEXT.

    Endpoint note (verified against Gupshup docs 2026-06-12): Gupshup's
    currently *documented* template-creation API is the Partner API
    (POST https://partner.gupshup.io/partner/app/{appId}/templates,
    https://docs.gupshup.io/reference/post_partner-app-appid-templates-8),
    which authenticates with a partner app token + appId rather than the
    platform ``apikey`` + ``appName`` this library uses (see
    sm_common.gupshup.GupshupClient). No self-serve apikey-based create
    endpoint is documented at docs.gupshup.io. This function follows the
    apikey/appName self-serve convention against ``{base_url}/template``;
    pass ``base_url`` to target a different deployment, and verify the
    endpoint against your Gupshup account before production use.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{base_url}/template",
            headers={"apikey": api_key},
            data={
                "appName": app_name,
                "elementName": template_name,
                "category": "UTILITY",
                "templateType": "TEXT",
                "content": body,
                "channel": channel,
            },
        )
        response.raise_for_status()
        data = response.json()
        template = data.get("template") if isinstance(data, dict) else None
        dlt_id = template.get("id") if isinstance(template, dict) else None
        if isinstance(dlt_id, bool) or not isinstance(dlt_id, (str, int)) or not str(dlt_id):
            detail = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise ValueError(f"Gupshup template response missing id (keys: {detail})")
        return str(dlt_id)
