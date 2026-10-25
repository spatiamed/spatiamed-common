import pytest

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.exceptions import ConfigError, HmsAdapterError

REQ = {"location_uuid": "L", "identifier_type_uuid": "T", "identifier_source_uuid": "S"}


def test_config_from_mapping_ignores_unknown_keys():
    cfg = BahmniConfig.from_mapping({**REQ, "record_mode": True, "documents_enabled": False})
    assert cfg.location_uuid == "L"
    assert cfg.identifier_type_uuid == "T"
    assert cfg.phone_attribute_name == "phoneNumber"
    assert "UNKNOWN" in cfg.system_provider_identifiers


def test_config_from_mapping_coerces_collections():
    cfg = BahmniConfig.from_mapping(
        {**REQ, "service_by_practitioner": {"p1": "s1"}, "system_provider_identifiers": ["X"]}
    )
    assert cfg.service_by_practitioner == {"p1": "s1"}
    assert cfg.system_provider_identifiers == frozenset({"X"})


def test_config_from_mapping_missing_required_raises_config_error():
    with pytest.raises(ConfigError, match="location_uuid"):
        BahmniConfig.from_mapping({"identifier_type_uuid": "T"})
    with pytest.raises(ConfigError):
        BahmniConfig.from_mapping(None)
    assert issubclass(ConfigError, HmsAdapterError)


def test_service_for_falls_back_then_raises():
    cfg = BahmniConfig.from_mapping(
        {**REQ, "service_by_practitioner": {"p1": "svc-1"}, "default_service_uuid": "svc-d"}
    )
    assert cfg.service_for("p1") == "svc-1"
    assert cfg.service_for("other") == "svc-d"
    assert cfg.service_for(None) == "svc-d"
    bare = BahmniConfig.from_mapping(REQ)
    with pytest.raises(ConfigError):
        bare.service_for("p1")


def test_drug_quantity_units_defaults_and_overrides():
    req = {"location_uuid": "L", "identifier_type_uuid": "T", "identifier_source_uuid": "S"}
    assert BahmniConfig.from_mapping(req).drug_quantity_units == "Unit"
    assert (
        BahmniConfig.from_mapping({**req, "drug_quantity_units": "Tablet"}).drug_quantity_units
        == "Tablet"
    )
