"""The Bahmni live harness lives in a PUBLIC repo: no default credentials, no AWS ids."""

from __future__ import annotations

import pathlib
import re

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"


def _sources() -> dict[str, str]:
    assert HARNESS.is_dir(), f"{HARNESS} missing"
    files = sorted(HARNESS.glob("*.py"))
    assert files, "harness/bahmni has no .py files"
    return {f.name: f.read_text() for f in files}


def test_harness_has_no_default_credentials() -> None:
    for name, text in _sources().items():
        for word in ("Admin123", "superman"):
            assert word not in text, f"{name} contains {word!r}"


def test_harness_has_no_aws_ids() -> None:
    patterns = [r"i-0[0-9a-f]{8,}", r"sg-0", r"subnet-0", r"\b\d{12}\b"]
    texts = _sources()
    readme = HARNESS / "README.md"
    if readme.exists():
        texts["README.md"] = readme.read_text()
    for name, text in texts.items():
        for pat in patterns:
            assert not re.search(pat, text), f"{name} matches {pat!r}"


# ─── The --adt group ────────────────────────────────────────────────────────

# THE list: every CHECK line the --adt group must print, with the topic it verifies.
# Change it only together with harness/bahmni/adt_checks.py and its README.
ADT_CHECK_TOPIC = {
    "module_versions": "stack_module_versions",
    "adt_config_resolved": "adt_config_keys,disposition_mappings,worklist_queries",
    "disposition_mappings": "disposition_mappings",
    "type_names_exact": "encounter_type_names",
    "emr_gps_set": "inpatient_visit_settings",
    "unknown_ward_uuid": "unknown_ward_uuid",
    "ward_create": "ward_bed_layout",
    "ward_layout": "ward_bed_layout",
    "ward_create_response_shape": "ward_create_response_shape",
    "bed_create_response_shape": "bed_number_max_len",
    "ward_name_case": "ward_name_case",
    "ward_layout_shrink": "ward_layout_shrink",
    "ward_location_search_rep": "ward_location_search_rep",
    "ward_duplicate_name_400_shape": "ward_duplicate_name_refusal",
    "ward_layout_on_untagged_location": "ward_untagged_location",
    "bed_create_orphan_on_cell_conflict": "bed_create_orphan",
    "bedtype_name_case": "bedtype_name_case",
    "location_duplicate_retired_first": "location_duplicate_retired",
    "find_by_marker_live": "find_by_marker",
    "disposition_admit_lists_to_admit": "admit_worklist",
    "admit_new_ipd_visit": "admit_encounter",
    "admit_retry_same_encounter": "admit_encounter",
    "bed_occupied_after_admit": "bed_single_occupancy",
    "adt_visit_rep_parses": "adt_visit_rep",
    "visit_uuid_kept": "admission_visit_reuse",
    "second_patient_same_bed_refused_by_client": "bed_single_occupancy",
    "bed_read_shape": "bed_read_by_id",
    "bed_assign_response_shape": "bed_assign_response_shape",
    "bed_assign_unknown_id_rollback": "bed_assign_rollback",
    "transfer_moves_bed": "transfer_bed_move",
    "transfer_merge_window": "transfer_bed_move",
    "child_location_bed_current_bed": "child_location_bed",
    "disposition_discharge_lists_to_discharge": "discharge_flow",
    "discharge_frees_bed": "discharge_flow",
    "undo_discharge": "undo_discharge",
    "voided_encounter_visibility": "voided_encounter_visibility",
    "discharge_summary_type": "encounter_type_names",
    "discharge_visit_still_open_then_end_visit": "discharge_flow",
    "discharge_summary_after_visit_end": "discharge_summary_after_visit_end",
    "visit_list_order": "visit_list_order",
    "admission_merge_window": "admission_merge_window",
    "encounter_match_multiple": "encounter_match_multiple",
    "admit_conversion_atomic": "admit_conversion_atomic",
    "ipd_side_effects": "ipd_side_effects",
    "feed_titles": "encounter_feed_titles",
    "worklist_misconfig": "worklist_misconfig",
    "ward_admission_tag_missing": "ward_admission_tag",
    "admitted_visit_survives_stale_close": "inpatient_visit_settings",
}
# A uuid literal: RFC 4122 shape, or OpenMRS/CIEL-style 32-38 alnum runs holding a digit.
UUID_LITERAL = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\b(?=[A-Za-z]*\d)[0-9A-Za-z]{32,38}\b"
)


def test_harness_adt_group_has_no_hardcoded_uuids() -> None:
    src = _sources()
    assert "adt_checks.py" in src, "the --adt group lives in harness/bahmni/adt_checks.py"
    adt, verify = src["adt_checks.py"], src["verify_adapter_live.py"]
    assert '"--adt"' in verify, "verify_adapter_live.py must take --adt"
    for name, text in (("adt_checks.py", adt), ("verify_adapter_live.py", verify)):
        hits = UUID_LITERAL.findall(text)
        assert not hits, f"{name} carries uuid literals: {hits[:3]}"
    # Every listed CHECK is registered in the group with its topic.
    head = "ADT_CHECKS: list[tuple[str, str, AdtCheck]] = ["
    registry = adt.split(head, 1)[1].split("\n]\n", 1)[0]
    registered = dict(
        re.findall(r'\(\s*"([a-z0-9_]+)",\s*"([a-z0-9_]+(?:,[a-z0-9_]+)*)",', registry)
    )
    missing = sorted(set(ADT_CHECK_TOPIC) - set(registered))
    assert not missing, f"--adt group lacks CHECKs: {missing}"
    extra = sorted(set(registered) - set(ADT_CHECK_TOPIC))
    assert not extra, f"--adt group registers CHECKs the list does not name: {extra}"
    wrong = {n: registered[n] for n, topic in ADT_CHECK_TOPIC.items() if registered[n] != topic}
    assert not wrong, f"CHECK -> topic mapping differs from ADT_CHECK_TOPIC: {wrong}"
    # The ADT group works on its own LT-P2- patients, never the seed patient.
    assert 'h["patient_uuid"]' not in adt
    assert "LT-P2-" in adt
