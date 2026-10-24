"""Per-integration configuration for the Bahmni adapter.

Everything here is passed in by the caller (typically from an integration's
``field_mapping``); the library reads no environment variables and ships no
default identifiers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from sm_common.integrations.exceptions import ConfigError

_REQUIRED = ("location_uuid", "identifier_type_uuid", "identifier_source_uuid")
_MAPPING_FIELDS = ("service_by_practitioner", "concept_map", "document_type_concepts")


@dataclass(frozen=True)
class BahmniConfig:
    location_uuid: str
    identifier_type_uuid: str
    identifier_source_uuid: str
    # Person attribute type NAME used by bahmni/search.
    phone_attribute_name: str = "phoneNumber"
    phone_attribute_type_uuid: str = ""
    # Person attribute holding our patients.id ("" = no marker).
    marker_attribute_name: str = ""
    marker_attribute_type_uuid: str = ""
    default_service_uuid: str = ""
    # provider uuid -> appointment service uuid
    service_by_practitioner: Mapping[str, str] = field(default_factory=dict)
    opd_visit_type_uuid: str = ""
    consultation_encounter_type_uuid: str = ""
    document_encounter_type_uuid: str = ""
    document_visit_type_uuid: str = ""
    # Provider used for document filing by staff with no practitioner mapping.
    default_provider_uuid: str = ""
    # Text concept carrying our clinical_note id on pushed encounters.
    note_marker_concept_uuid: str = ""
    # keys: chief_complaint, history, examination, advice, follow_up, vitals_pulse,
    # vitals_sbp, vitals_dbp, vitals_temp, vitals_spo2, vitals_weight, vitals_height
    concept_map: Mapping[str, str] = field(default_factory=dict)
    drug_other_concept_uuid: str = ""
    # our doc_type -> Bahmni concept uuid
    document_type_concepts: Mapping[str, str] = field(default_factory=dict)
    system_provider_identifiers: frozenset[str] = frozenset(
        {"UNKNOWN", "LABMANAGER", "LABSYSTEM", "superman"}
    )

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> BahmniConfig:
        """Build from an integration field mapping; unknown keys are ignored."""
        src = m or {}
        missing = [k for k in _REQUIRED if not src.get(k)]
        if missing:
            raise ConfigError(f"bahmni config missing required keys: {', '.join(missing)}")
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {k: v for k, v in src.items() if k in known}
        for k in _MAPPING_FIELDS:
            if k in kwargs:
                kwargs[k] = dict(kwargs[k] or {})
        if "system_provider_identifiers" in kwargs:
            kwargs["system_provider_identifiers"] = frozenset(kwargs["system_provider_identifiers"])
        return cls(**kwargs)

    def service_for(self, provider_uuid: str | None) -> str:
        """Appointment service for a provider: mapped, else the default, else ConfigError."""
        if provider_uuid and provider_uuid in self.service_by_practitioner:
            return self.service_by_practitioner[provider_uuid]
        if self.default_service_uuid:
            return self.default_service_uuid
        raise ConfigError(
            f"bahmni: no appointment service for provider {provider_uuid!r} and no default_service_uuid"
        )
