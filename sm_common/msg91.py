from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class MSG91Config:
    auth_key: str  # Platform-level MSG91 auth key
    base_url: str = "https://control.msg91.com/api/v5"


class MSG91Client:
    """SMS API client via MSG91. DLT-registered templates only."""

    def __init__(self, config: MSG91Config):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def send_sms(
        self,
        to: str,
        template_id: str,
        params: dict[str, str],
        sender_id: str,
    ) -> dict[str, Any]:
        """Send a DLT-registered SMS.

        Args:
            to: Recipient phone (normalized 10-digit)
            template_id: DLT template ID
            params: Template variable values {"var1": "value", "var2": "value"}
            sender_id: 6-char sender ID (e.g. "CITYGH")
        """
        response = await self._client.post(
            f"{self.config.base_url}/flow",
            headers={"authkey": self.config.auth_key},
            json={
                "template_id": template_id,
                "sender": sender_id,
                "recipients": [{"mobiles": f"91{to}", **params}],
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    async def close(self) -> None:
        await self._client.aclose()
