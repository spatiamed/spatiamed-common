import httpx
from dataclasses import dataclass


@dataclass
class TruecallerConfig:
    partner_key: str      # Truecaller Business partner key
    base_url: str = "https://api.truecaller.com/v1/business"


class TruecallerClient:
    """Register hospital phone numbers with Truecaller for verified caller ID."""

    def __init__(self, config: TruecallerConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def register_numbers(
        self, numbers: list[str], business_name: str,
        category: str = "Hospital", logo_url: str | None = None,
    ) -> dict:
        """Register DIDs with Truecaller so calls show hospital name."""
        response = await self._client.post(
            f"{self.config.base_url}/register",
            headers={"Authorization": f"Bearer {self.config.partner_key}"},
            json={
                "phoneNumbers": numbers,
                "businessName": business_name,
                "category": category,
                "logoUrl": logo_url,
            },
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()
