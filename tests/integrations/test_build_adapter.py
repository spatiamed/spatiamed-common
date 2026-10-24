import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.exceptions import ConfigError
from sm_common.integrations.build_adapter import (
    AdapterBuildConfig,
    build_adapter,
    build_bahmni_record_client,
)


def _cfg(**kw):
    base = dict(vendor="fhir_r4", base_url="https://h/fhir", credentials={}, field_mapping=None)
    base.update(kw)
    return AdapterBuildConfig(**base)


def test_builds_fhir_adapter():
    a = build_adapter(
        _cfg(vendor="fhir_r4", credentials={"auth_scheme": "bearer", "bearer_token": "t"})
    )
    assert isinstance(a, FhirR4Adapter)


def test_builds_generic_rest_with_mapping():
    a = build_adapter(
        _cfg(
            vendor="generic_rest",
            field_mapping={"auth_scheme": "api_key"},
            credentials={"api_key": "K"},
        )
    )
    assert isinstance(a, GenericRestAdapter)


def test_unknown_vendor_raises():
    with pytest.raises(ValueError, match="Unknown HMS vendor"):
        build_adapter(_cfg(vendor="nope"))


def test_build_openemr_adapter():
    creds = {
        "auth_scheme": "private_key_jwt",
        "token_url": "t",
        "client_id": "c",
        "private_key_pem": "k",
        "openemr": {
            "timezone": "Asia/Kolkata",
            "pc_catid": "5",
            "pc_facility": "3",
            "pc_billing_location": "3",
            "write_user": {"token_url": "t"},
        },
    }
    a = build_adapter(
        AdapterBuildConfig(
            vendor="openemr",
            base_url="https://oe/apis/default/fhir",
            credentials=creds,
            field_mapping=None,
        )
    )
    assert a.vendor_name == "openemr"
    assert a._std == "https://oe/apis/default/api"
    assert "openemr" not in a._cfg  # system auth cfg must not carry the write credential


def test_token_cache_is_wired_to_fhir_system_auth_cfg():
    from sm_common.integrations.auth import InMemoryTokenCache

    cache = InMemoryTokenCache()
    a = build_adapter(
        _cfg(
            credentials={"auth_scheme": "oauth2_client_credentials", "token_url": "t"},
            token_cache=cache,
        )
    )
    assert a._cfg["_token_cache"] is cache
    assert a._cfg["_token_slot"] == "system"


def test_token_cache_is_wired_to_both_openemr_identities_in_separate_slots():
    from sm_common.integrations.auth import InMemoryTokenCache

    cache = InMemoryTokenCache()
    write_user = {"token_url": "t", "username": "u"}
    creds = {
        "auth_scheme": "private_key_jwt",
        "token_url": "t",
        "client_id": "c",
        "private_key_pem": "k",
        "openemr": {
            "timezone": "Asia/Kolkata",
            "pc_catid": "5",
            "pc_facility": "3",
            "pc_billing_location": "3",
            "write_user": write_user,
        },
    }
    a = build_adapter(
        AdapterBuildConfig(
            vendor="openemr",
            base_url="https://oe/apis/default/fhir",
            credentials=creds,
            field_mapping=None,
            token_cache=cache,
        )
    )
    assert a._cfg["_token_cache"] is cache and a._cfg["_token_slot"] == "system"
    assert a._write_cfg["_token_cache"] is cache and a._write_cfg["_token_slot"] == "write_user"
    assert "_token_cache" not in write_user  # the caller's credentials dict is not mutated


def test_no_token_cache_leaves_auth_cfg_unchanged():
    a = build_adapter(
        _cfg(credentials={"auth_scheme": "oauth2_client_credentials", "token_url": "t"})
    )
    assert "_token_cache" not in a._cfg


BAHMNI_MAPPING = {
    "location_uuid": "L",
    "identifier_type_uuid": "T",
    "identifier_source_uuid": "S",
    "record_mode": True,
}
BAHMNI_CREDS = {"username": "u", "password": "p"}


def _bahmni_cfg(creds: dict, field_mapping: dict | None = None) -> AdapterBuildConfig:  # type: ignore[type-arg]
    return AdapterBuildConfig(
        vendor="bahmni",
        base_url="https://bahmni.example.test",
        credentials=creds,
        field_mapping=field_mapping if field_mapping is not None else BAHMNI_MAPPING,
    )


def test_api_key_scheme_rejected():
    with pytest.raises(ValueError, match="api_key auth is not supported by OpenMRS; use basic"):
        build_adapter(_bahmni_cfg({"auth_scheme": "api_key", "api_key": "k"}))


@pytest.mark.parametrize("creds", [{}, {"auth_scheme": "basic"}, {"auth_scheme": "session"}])
def test_basic_default_and_session_alias_accepted(creds):
    a = build_adapter(_bahmni_cfg({**creds, **BAHMNI_CREDS}))
    assert a.vendor_name == "bahmni"
    assert a._config.location_uuid == "L"


def test_unknown_bahmni_scheme_rejected():
    with pytest.raises(ValueError, match="unknown auth_scheme"):
        build_adapter(_bahmni_cfg({"auth_scheme": "bearer"}))


def test_bahmni_record_client_requires_bahmni_vendor():
    with pytest.raises(ValueError, match="vendor 'bahmni'"):
        build_bahmni_record_client(_cfg(vendor="fhir_r4"))


def test_bahmni_missing_config_or_credentials_is_config_error():
    # field_mapping=None is not "use defaults": the uuids have no safe default.
    cfg = AdapterBuildConfig(
        vendor="bahmni",
        base_url="https://b.example.test",
        credentials=BAHMNI_CREDS,
        field_mapping=None,
    )
    with pytest.raises(ConfigError, match="location_uuid"):
        build_adapter(cfg)
    with pytest.raises(ConfigError, match="username and password"):
        build_adapter(_bahmni_cfg({}))


def test_bahmni_tls_options_reach_http():
    a = build_adapter(
        _bahmni_cfg({**BAHMNI_CREDS, "verify_tls": False, "allow_insecure_tls": True})
    )
    assert a._http.verify is False
    with pytest.raises(ValueError, match="allow_insecure_tls"):
        build_adapter(_bahmni_cfg({**BAHMNI_CREDS, "verify_tls": False}))
    assert build_adapter(_bahmni_cfg(BAHMNI_CREDS))._http.verify is True
