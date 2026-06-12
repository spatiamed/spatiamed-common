"""Truecaller Business caller-ID registration client.

STATUS (2026-06-12): no service consumes this yet. CareLoop's
external/caller_id_provisioner.py is the planned integration point.
Delete this module if caller-ID provisioning is dropped from the roadmap.
"""
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class TruecallerConfig:
    partner_key: str  # Truecaller Business partner key
    base_url: str = "https://api.truecaller.com/v1/business"


class TruecallerClient:
    """Register hospital phone numbers with Truecaller for verified caller ID."""

    def __init__(self, config: TruecallerConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def register_numbers(
        self,
        numbers: list[str],
        business_name: str,
        category: str = "Hospital",
        logo_url: str | None = None,
    ) -> dict[str, Any]:
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
        data: dict[str, Any] = response.json()
        return data

    async def close(self) -> None:
        await self._client.aclose()
