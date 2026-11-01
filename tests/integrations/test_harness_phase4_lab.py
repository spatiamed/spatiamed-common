"""Offline checks of the lab result harness group (``harness/bahmni/phase4_lab.py``).

The group only runs live against Bahmni; here it runs against in-process fakes so its
shape (one row per check, unique names) and its key guard (the newer-result check must
FAIL when a write was posted) are pinned without a server. The group lives in a PUBLIC
repo: no hosts, no env reads, no uuid literals.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_lab_results import LabResultsConfig
from sm_common.integrations.lab_results_types import LabResultWriteOutcome

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"
sys.path.insert(0, str(HARNESS))
import adt_checks  # noqa: E402
import phase4_lab  # noqa: E402

EXPECTED = [
    "lab_result_types_resolved",
    "lab_result_write_readback",
    "lab_result_panel_readback",
    "lab_result_newer_guard",
    "lab_result_session_merge",
    "lab_result_no_clobber_after_collect",
    "lab_result_void_amend",
]
UUID_LITERAL = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\b(?=[A-Za-z]*\d)[0-9A-Za-z]{32,38}\b"
)


def _u() -> str:
    return str(uuid4())


def _session(req: httpx.Request) -> httpx.Response | None:
    if req.url.path.endswith("/session"):
        return httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=x"}
        )
    return None


def _ctx(handler: Any, tmp_path: pathlib.Path, monkeypatch: Any) -> adt_checks.AdtCtx:
    monkeypatch.setenv("HARNESS_SAMPLES_DIR", str(tmp_path))
    http = BahmniHttp("https://b.test", "u", "p", transport=httpx.MockTransport(handler))
    config = BahmniConfig(location_uuid=_u(), identifier_type_uuid="t", identifier_source_uuid="s")
    return adt_checks.AdtCtx({"harness": {"provider_uuid": _u()}}, http, config, http)


async def test_run_checks_returns_one_row_per_check(tmp_path, monkeypatch):
    async def handle(req: httpx.Request) -> httpx.Response:
        return _session(req) or httpx.Response(404, json={})

    x = _ctx(handle, tmp_path, monkeypatch)
    rows = await phase4_lab.run_checks(x)
    assert [name for name, _topic, _res in rows] == EXPECTED
    for _name, topic, (status, detail) in rows:
        assert topic and re.fullmatch(r"[a-z0-9_]+", topic)
        assert status in {"PASS", "FAIL", "INCONCLUSIVE", "SKIP"}
        assert isinstance(detail, str)
    # Nothing resolves on an empty server: the gate fails and every later check is blocked.
    assert rows[0][2][0] == "FAIL"
    assert all(res[0] == "INCONCLUSIVE" for _n, _t, res in rows[1:])


def test_check_names_unique():
    names = [name for name, _topic, _fn in phase4_lab.LAB_CHECKS]
    assert names == EXPECTED
    assert len(set(names)) == len(names)
    for _name, topic, _fn in phase4_lab.LAB_CHECKS:
        assert "LV" not in topic.upper().split("_")


class _Box:
    """A tiny labOrderResults + bahmniencounter fake for one resulted order."""

    def __init__(self, *, record_has_newer: bool) -> None:
        self.patient, self.visit, self.order, self.test = _u(), _u(), _u(), _u()
        self.first_at = datetime.now(UTC).replace(microsecond=0)
        self.record_has_newer = record_has_newer
        self.posts: list[dict[str, Any]] = []

    def row(self) -> dict[str, Any]:
        resulted = self.record_has_newer
        return {
            "orderUuid": self.order,
            "testUuid": self.test,
            "testName": "Test Haemoglobin",
            "result": "12" if resulted else None,
            "minNormal": 1 if resulted else None,
            "maxNormal": 10 if resulted else None,
            "abnormal": True if resulted else None,
            "provider": "Test Provider" if resulted else None,
            "resultDateTime": int(self.first_at.timestamp() * 1000) if resulted else None,
        }

    async def handle(self, req: httpx.Request) -> httpx.Response:
        if (hit := _session(req)) is not None:
            return hit
        if req.url.path.endswith("/bahmnicore/labOrderResults"):
            return httpx.Response(200, json={"results": [self.row()]})
        if req.method == "POST" and req.url.path.endswith("/bahmnicore/bahmniencounter"):
            body = json.loads(req.content)
            self.posts.append(body)
            obs = [
                {
                    "uuid": _u(),
                    "concept": o["concept"],
                    "orderUuid": o["orderUuid"],
                    "observationDateTime": o["observationDateTime"],
                    "voided": False,
                }
                for o in body["observations"]
            ]
            return httpx.Response(
                200,
                json={"encounterUuid": body.get("encounterUuid") or _u(), "observations": obs},
            )
        return httpx.Response(404, json={})


def _seed_single(x: adt_checks.AdtCtx, box: _Box) -> None:
    x.state["lab"] = {
        "config": LabResultsConfig(
            lab_result_encounter_type_uuid=_u(),
            concept_lab_abnormal_uuid=_u(),
            concept_lab_minnormal_uuid=_u(),
            concept_lab_maxnormal_uuid=_u(),
            concept_lab_notes_uuid=_u(),
            concept_referred_out_uuid=_u(),
            result_location_uuid=_u(),
        ),
        "single": {
            "patient": box.patient,
            "visit": box.visit,
            "order": box.order,
            "test": box.test,
            "result_at": box.first_at,
            "value": Decimal("12"),
            "outcome": LabResultWriteOutcome(encounter_uuid=_u(), obs_uuids_by_order={}),
        },
    }


async def test_newer_guard_check_fails_when_post_seen(tmp_path, monkeypatch):
    # The record shows the order still pending, so the client posts the older write:
    # the check must report the guard as broken, and clean up what was written.
    box = _Box(record_has_newer=False)
    x = _ctx(box.handle, tmp_path, monkeypatch)
    _seed_single(x, box)
    status, detail = await phase4_lab.lab_result_newer_guard(x)
    assert box.posts, "the fake must have seen the POST"
    assert status == "FAIL"
    assert "guard broken" in detail
    assert any(c.kind == "obs" for c in x.cleanups), "a written group is voided at cleanup"


async def test_newer_guard_check_passes_when_refused_without_post(tmp_path, monkeypatch):
    box = _Box(record_has_newer=True)
    x = _ctx(box.handle, tmp_path, monkeypatch)
    _seed_single(x, box)
    status, detail = await phase4_lab.lab_result_newer_guard(x)
    assert not box.posts
    assert status == "PASS", detail
    assert "record_result_newer" in detail


async def test_no_clobber_is_inconclusive_without_collected_order(tmp_path, monkeypatch):
    box = _Box(record_has_newer=True)
    x = _ctx(box.handle, tmp_path, monkeypatch)
    _seed_single(x, box)
    status, detail = await phase4_lab.lab_result_no_clobber_after_collect(x)
    assert status == "INCONCLUSIVE"
    assert "lab_collect" in detail


def test_feed_wait_covers_two_cycles():
    assert phase4_lab.FEED_WAIT_S >= 30


def test_no_host_or_env_literals():
    src = (HARNESS / "phase4_lab.py").read_text()
    for banned in ("os.environ", "getenv", "http://", "https://", "localhost", ".internal"):
        assert banned not in src, f"phase4_lab.py contains {banned!r}"
    assert not UUID_LITERAL.findall(src), "phase4_lab.py carries uuid literals"
    assert not re.search(r"\bLV-|\bP\d-LV", src), "no check-plan labels in a public file"
    # Synthetic records only: the group creates its patients through the shared helper.
    assert "new_patient(" in src
    assert 'h["patient_uuid"]' not in src


def test_result_times_are_older_than_now_for_guard():
    # The guard write sits well before the first result (not within the same-instant
    # second the client treats as a retry of the same write).
    assert timedelta(minutes=1) <= phase4_lab.GUARD_OFFSET


@pytest.mark.parametrize("name", EXPECTED)
def test_every_check_is_async(name):
    import inspect

    fn = dict((n, f) for n, _t, f in phase4_lab.LAB_CHECKS)[name]
    assert inspect.iscoroutinefunction(fn)
