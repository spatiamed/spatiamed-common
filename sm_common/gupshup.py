import httpx
from dataclasses import dataclass


@dataclass
class GupshupConfig:
    api_key: str          # Platform-level Gupshup API key
    app_name: str         # "spatiamed" or tenant-specific sub-app
    base_url: str = "https://api.gupshup.io/wa/api/v1"


class GupshupClient:
    """WhatsApp Business API client via Gupshup.

    Used by both QueueCare notification service and CareLoop campaign engine.
    Callers provide the tenant's assigned WhatsApp number and Gupshup app name.
    """

    def __init__(self, config: GupshupConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def send_template(
        self, to: str, template_id: str, params: list[str],
        source: str, app_name: str | None = None,
    ) -> dict:
        """Send a pre-approved DLT template message.

        Args:
            to: Recipient phone (normalized 10-digit)
            template_id: Gupshup/DLT template identifier
            params: Template parameter values
            source: Sender WhatsApp number (tenant's assigned number)
            app_name: Gupshup sub-app for this tenant (overrides default)
        """
        response = await self._client.post(
            f"{self.config.base_url}/msg",
            headers={"apikey": self.config.api_key},
            data={
                "channel": "whatsapp",
                "source": source,
                "destination": f"91{to}",
                "src.name": app_name or self.config.app_name,
                "template": template_id,
                "params": ",".join(params),
            },
        )
        response.raise_for_status()
        return response.json()

    async def send_session_message(
        self, to: str, text: str, source: str,
        app_name: str | None = None,
    ) -> dict:
        """Send a free-form session message (within 24h window)."""
        response = await self._client.post(
            f"{self.config.base_url}/msg",
            headers={"apikey": self.config.api_key},
            data={
                "channel": "whatsapp",
                "source": source,
                "destination": f"91{to}",
                "src.name": app_name or self.config.app_name,
                "message": text,
            },
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()
