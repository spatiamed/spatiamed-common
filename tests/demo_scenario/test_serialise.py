# tests/demo_scenario/test_serialise.py
from __future__ import annotations

import json
import os
import uuid
from datetime import date

from sm_common.demo_scenario import ScenarioConfig
from sm_common.demo_scenario.serialise import SCHEMA_VERSION, scenario_to_dict

CFG = ScenarioConfig(
    tenant_id=uuid.UUID("6ce47f21-64b6-4298-aebb-b8845648c8c0"),
    start=date(2026, 6, 29),
    end=date(2026, 7, 5),
)


def test_top_level_shape():
    doc = scenario_to_dict(CFG)
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["tenant_id"] == str(CFG.tenant_id)
    assert doc["start"] == "2026-06-29"
    assert doc["end"] == "2026-07-05"
    assert set(doc) == {
        "schema_version", "tenant_id", "seed", "start", "end", "patients", "days",
    }


def test_patient_records_carry_every_field_the_backfill_needs():
    patient = scenario_to_dict(CFG)["patients"][0]
    assert set(patient) == {"index", "patient_id", "name", "phone", "age", "gender", "language"}


def test_token_records_carry_every_field_the_backfill_needs():
    days = scenario_to_dict(CFG)["days"]
    token = next(t for d in days for t in d["tokens"])
    assert set(token) == {
        "sequence_number", "patient_index", "department_code", "doctor_slot",
        "arrival", "called_at", "started_at", "completed_at", "outcome",
        "payment_method", "amount_inr", "feedback_rating", "note_text",
        "diagnosis_term", "icd_code",
    }


def test_timestamps_serialise_as_iso_utc():
    days = scenario_to_dict(CFG)["days"]
    token = next(t for d in days for t in d["tokens"])
    assert token["arrival"].endswith("+00:00")


def test_document_is_json_round_trippable_and_stable():
    first = json.dumps(scenario_to_dict(CFG), sort_keys=True)
    assert first == json.dumps(scenario_to_dict(CFG), sort_keys=True)
    assert json.loads(first)["schema_version"] == SCHEMA_VERSION


def test_scenario_is_byte_identical_across_interpreter_processes(tmp_path):
    """The contract is cross-PROCESS determinism, not just repeat calls in one process.

    Two consumers in different repos generate this artifact independently; if they
    disagree, the backfill and the live driver describe different worlds. A same-process
    repeat check would still pass if generation depended on hash(), which is per-run.
    """
    import hashlib
    import subprocess
    import sys

    script = (
        "import json,uuid,datetime;"
        "from sm_common.demo_scenario import ScenarioConfig;"
        "from sm_common.demo_scenario.serialise import scenario_to_dict;"
        "cfg=ScenarioConfig(tenant_id=uuid.UUID('6ce47f21-64b6-4298-aebb-b8845648c8c0'),"
        "start=datetime.date(2026,6,29),end=datetime.date(2026,7,5));"
        "print(json.dumps(scenario_to_dict(cfg),sort_keys=True))"
    )

    digests = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": "random"},
        )
        digests.append(hashlib.sha256(result.stdout.encode()).hexdigest())

    assert digests[0] == digests[1]
