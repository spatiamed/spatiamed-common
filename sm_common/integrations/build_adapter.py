"""Unified HMS adapter factory.

Selects the right HmsAdapter subclass from an AdapterBuildConfig, replacing
the per-service factories in QueueCare and hms-connector-agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.adapters.mocdoc import MocDocAdapter
from sm_common.integrations.adapters.openemr import OpenEmrAdapter
from sm_common.integrations.auth import TokenCache
from sm_common.integrations.exceptions import ConfigError
from sm_common.integrations.hms_adapter import HmsAdapter


@dataclass
class AdapterBuildConfig:
    vendor: str
    base_url: str
    credentials: dict  # type: ignore[type-arg]
    field_mapping: dict | None  # type: ignore[type-arg]
    hash_salt: str = ""
    transport_key: str = ""
    # Caller-owned OAuth token store (see sm_common.integrations.auth.TokenCache).
    # Without it a token lives only as long as the adapter. Pass one instance per
    # integration: whatever is in it is sent to this integration's HMS.
    token_cache: TokenCache | None = None


def _with_cache(auth_cfg: dict, cache: TokenCache | None, slot: str) -> dict:  # type: ignore[type-arg]
    if cache is None:
        return auth_cfg
    return {**auth_cfg, "_token_cache": cache, "_token_slot": slot}


def _check_bahmni_auth_scheme(scheme: str) -> None:
    if scheme == "api_key":
        raise ValueError("bahmni: api_key auth is not supported by OpenMRS; use basic")
    if scheme not in ("basic", "session"):
        raise ValueError(f"bahmni: unknown auth_scheme {scheme!r}; use basic")


def build_bahmni_record_client(cfg: AdapterBuildConfig) -> Any:
    """Build the Bahmni record client (BahmniRecordClient). Filled in by Plan 02 Task 3."""
    if cfg.vendor.lower() != "bahmni":
        raise ValueError(f"build_bahmni_record_client needs vendor 'bahmni', got {cfg.vendor!r}")
    raise NotImplementedError("BahmniRecordClient is not implemented yet")


def build_adapter(cfg: AdapterBuildConfig) -> HmsAdapter:
    """Return an initialised HmsAdapter for the given config.

    ``credentials`` must be plaintext — callers decrypt before calling this.
    """
    creds: dict = cfg.credentials or {}  # type: ignore[type-arg]
    base_url = cfg.base_url
    hash_salt = cfg.hash_salt
    transport_key = cfg.transport_key

    vendor = cfg.vendor.lower()

    if vendor == "bahmni":
        # OpenMRS only does basic/session auth; "basic" and "session" are aliases.
        _check_bahmni_auth_scheme(creds.get("auth_scheme", "basic"))
        if not creds.get("username") or not creds.get("password"):
            raise ConfigError("bahmni: credentials need username and password")
        return BahmniAdapter(
            http=BahmniHttp(
                base_url or creds.get("base_url", ""),
                creds["username"],
                creds["password"],
                ca_bundle_pem=creds.get("ca_bundle_pem"),
                verify_tls=creds.get("verify_tls", True),
                allow_insecure_tls=creds.get("allow_insecure_tls", False),
            ),
            config=BahmniConfig.from_mapping(cfg.field_mapping),
            hash_salt=hash_salt,
            transport_key=transport_key,
        )

    if vendor == "mocdoc":
        return MocDocAdapter(
            base_url=base_url or creds.get("base_url", ""),
            api_key=creds.get("api_key", ""),
            api_secret=creds.get("api_secret", ""),
            hash_salt=hash_salt,
            transport_key=transport_key,
        )

    if vendor == "generic_rest":
        mapping: dict = dict(cfg.field_mapping or {})  # type: ignore[type-arg]
        mapping.setdefault("base_url", base_url or creds.get("base_url", ""))
        mapping["api_key"] = creds.get("api_key", mapping.get("api_key", ""))
        mapping["api_secret"] = creds.get("api_secret", mapping.get("api_secret", ""))
        return GenericRestAdapter(mapping)

    if vendor == "openemr":
        auth_scheme = creds.get("auth_scheme", "private_key_jwt")
        auth_cfg = {k: v for k, v in creds.items() if k not in ("auth_scheme", "openemr")}
        openemr: dict = dict(creds.get("openemr") or {})  # type: ignore[type-arg]
        if isinstance(openemr.get("write_user"), dict):
            openemr["write_user"] = _with_cache(
                openemr["write_user"], cfg.token_cache, "write_user"
            )
        return OpenEmrAdapter(
            base_url=base_url or creds.get("base_url", ""),
            auth_scheme=auth_scheme,
            auth_cfg=_with_cache(auth_cfg, cfg.token_cache, "system"),
            openemr=openemr,
            hash_salt=hash_salt,
        )

    if vendor == "fhir_r4":
        auth_scheme = creds.get("auth_scheme", "oauth2_client_credentials")
        auth_cfg = {k: v for k, v in creds.items() if k != "auth_scheme"}
        return FhirR4Adapter(
            base_url=base_url or creds.get("base_url", ""),
            auth_scheme=auth_scheme,
            auth_cfg=_with_cache(auth_cfg, cfg.token_cache, "system"),
            hash_salt=hash_salt,
        )

    raise ValueError(f"Unknown HMS vendor: {cfg.vendor!r}")
