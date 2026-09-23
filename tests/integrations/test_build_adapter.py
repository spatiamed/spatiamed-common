import pytest

from sm_common.integrations.adapters.fhir_r4 import FhirR4Adapter
from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.build_adapter import AdapterBuildConfig, build_adapter


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
