from dataclasses import dataclass
from typing import Any

import httpx


def _digits(value: str) -> str:
    """Digits-only form of a phone number, so '+91 98765 43210' == '919876543210'."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


@dataclass
class GupshupConfig:
    api_key: str  # Platform-level Gupshup API key
    app_name: str  # "spatiamed" or tenant-specific sub-app
    base_url: str = "https://api.gupshup.io/wa/api/v1"
    # Outbound fence. None = unrestricted (production). A set = only these
    # destinations may be messaged; an EMPTY set blocks everything, so a
    # misconfigured non-production deployment fails closed rather than
    # accept-all. Entries may be in any format — comparison is digits-only.
    allowed_destinations: frozenset[str] | None = None


class GupshupClient:
    """WhatsApp Business API client via Gupshup.

    Used by both QueueCare notification service and CareLoop campaign engine.
    Callers provide the tenant's assigned WhatsApp number and Gupshup app name.
    """

    def __init__(self, config: GupshupConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)
        self._allowed: frozenset[str] | None = (
            None
            if config.allowed_destinations is None
            else frozenset(_digits(entry) for entry in config.allowed_destinations)
        )

    def _blocked(self, destination: str) -> dict[str, Any] | None:
        """Return a block result when `destination` is fenced out, else None.

        Checked against the exact destination string the client is about to
        send, so the fence cannot be bypassed by an unexpected caller format.
        """
        if self._allowed is None:
            return None
        if _digits(destination) in self._allowed:
            return None
        return {
            "status": "blocked_by_allowlist",
            "messageId": "",
            "message": "destination not in allowlist",
        }

    async def send_template(
        self,
        to: str,
        template_id: str,
        params: list[str],
        source: str,
        app_name: str | None = None,
    ) -> dict[str, Any]:
        """Send a pre-approved DLT template message.

        Args:
            to: Recipient phone (normalized 10-digit)
            template_id: Gupshup/DLT template identifier
            params: Template parameter values
            source: Sender WhatsApp number (tenant's assigned number)
            app_name: Gupshup sub-app for this tenant (overrides default)
        """
        destination = f"91{to}"
        blocked = self._blocked(destination)
        if blocked is not None:
            return blocked

        response = await self._client.post(
            f"{self.config.base_url}/msg",
            headers={"apikey": self.config.api_key},
            data={
                "channel": "whatsapp",
                "source": source,
                "destination": destination,
                "src.name": app_name or self.config.app_name,
                "template": template_id,
                "params": ",".join(params),
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    async def send_session_message(
        self,
        to: str,
        text: str,
        source: str,
        app_name: str | None = None,
    ) -> dict[str, Any]:
        """Send a free-form session message (within 24h window)."""
        destination = f"91{to}"
        blocked = self._blocked(destination)
        if blocked is not None:
            return blocked

        response = await self._client.post(
            f"{self.config.base_url}/msg",
            headers={"apikey": self.config.api_key},
            data={
                "channel": "whatsapp",
                "source": source,
                "destination": destination,
                "src.name": app_name or self.config.app_name,
                "message": text,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    async def close(self) -> None:
        await self._client.aclose()
