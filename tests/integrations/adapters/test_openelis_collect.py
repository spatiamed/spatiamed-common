"""Lab-system (OpenELIS) read-only SQL transport + verified ``collect()``.

The lab database is simulated by ``FakeLab`` (a fake SQL executor keyed by the
library's fixed query texts) and the lab servlet by respx routes that apply the
collect call's documented behaviour to the same state: set the accession, cancel
every test missing from the list, publish an accession event.

All values are synthetic; the host is a reserved ``.test`` name.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid as uuidlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from sm_common.integrations.adapters import openelis
from sm_common.integrations.adapters.openelis import (
    LAB_QUERIES,
    AsyncpgLabExecutor,
    CollectResult,
    OpenElisClient,
    OpenElisConfig,
    OpenElisSql,
    PendingSample,
    parse_pg_duration,
)
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    NotFound,
    TransientError,
    VendorRejected,
)

BASE = "https://lab.example.test"
LOGIN = f"{BASE}/openelis/ValidateLogin.do"
AJAX = f"{BASE}/openelis/ajaxQueryXML"
DSN = "postgresql://lab_ro_test:pw-test@lab-db.example.test:5432/labdb"

CANCELED = 77  # arbitrary synthetic status ids, not any real install's values
NOT_TESTED = 71
SAMPLE_ID = 5001
OTHER_SAMPLE_ID = 5002
ENC = str(uuidlib.UUID("3f1c2a9e-6b2d-4c1e-9a77-0d5e8b1c4f21"))
OTHER_ENC = str(uuidlib.UUID("a8b7c6d5-1e2f-4a3b-8c9d-0e1f2a3b4c5d"))
ACC = "SM2610250001"
DAY = date(2026, 10, 25)


# ─── fake lab database + servlet ─────────────────────────────────────────────


@dataclass
class Analysis:
    id: int
    sample_id: int
    type_id: int
    test_id: int
    status_id: int = NOT_TESTED


@dataclass
class FakeLab:
    samples: dict[int, dict[str, Any]] = field(default_factory=dict)
    analyses: list[Analysis] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=lambda: [{"id": Decimal(CANCELED)}])
    type_names: dict[int, str] = field(default_factory=lambda: {4: "Blood", 9: "Urine"})
    test_names: dict[int, str] = field(
        default_factory=lambda: {
            12: "LT-P3-Haemoglobin",
            13: "LT-P3-Glucose",
            14: "LT-P3-Urea",
            31: "LT-P3-Urine routine",
        }
    )
    calls: list[str] = field(default_factory=list)
    next_event: int = 900

    def add_sample(
        self, sid: int, enc: str, tests: Mapping[int, list[int]], accession: str | None = None
    ) -> None:
        self.samples[sid] = {
            "id": sid,
            "uuid": enc,
            "accession_number": accession,
            "collection_date": None,
        }
        for type_id, test_ids in tests.items():
            for t in test_ids:
                self.add_analysis(sid, type_id, t)

    def add_analysis(
        self, sid: int, type_id: int, test_id: int, status: int = NOT_TESTED
    ) -> Analysis:
        a = Analysis(
            id=7000 + len(self.analyses),
            sample_id=sid,
            type_id=type_id,
            test_id=test_id,
            status_id=status,
        )
        self.analyses.append(a)
        return a

    def publish(self, enc: str) -> None:
        self.next_event += 1
        self.events.append(
            {"id": self.next_event, "uri": None, "object": f"/openelis/ws/rest/accession/{enc}"}
        )

    # The lab servlet's collect behaviour, applied to this state.
    def apply_collect(self, q: Mapping[str, str]) -> None:
        sid = int(q["sampleId"])
        body = json.loads(q["typeAndTestIds"])
        wanted = {
            (int(body["types"][k]), int(t))
            for k in body["types"]
            for t in body["tests"][k].split(",")
        }
        s = self.samples[sid]
        s["accession_number"] = q["accessionNumber"]
        d, m, y = q["collectionDate"].split("/")
        s["collection_date"] = datetime(int(y), int(m), int(d), 9, 30)
        for a in self.analyses:
            if a.sample_id == sid and (a.type_id, a.test_id) not in wanted:
                a.status_id = CANCELED
        self.publish(s["uuid"])

    def sample_tests_xml(self, sid: int) -> str:
        by_type: dict[int, list[int]] = {}
        for a in self.analyses:
            if a.sample_id == sid and a.status_id != CANCELED:
                by_type.setdefault(a.type_id, []).append(a.test_id)
        inner = "".join(
            f"<sample><sampleType>{t}</sampleType>"
            + "".join(f"<test>{x}</test>" for x in tests)
            + "</sample>"
            for t, tests in by_type.items()
        )
        return (
            f"<fieldmessage><formfield><samples>{inner}</samples></formfield>"
            "<message>valid</message></fieldmessage>"
        )

    # The fake SQL executor: dispatch on the library's fixed query text.
    async def fetch(self, sql: str, *args: Any) -> list[Mapping[str, Any]]:
        names = [k for k, v in LAB_QUERIES.items() if v == sql]
        assert len(names) == 1, f"unknown SQL reached the executor: {sql!r}"
        name = names[0]
        self.calls.append(name)
        return getattr(self, f"q_{name}")(*args)

    async def aclose(self) -> None:
        return None

    def q_canceled_status_id(self) -> list[Mapping[str, Any]]:
        return list(self.statuses)

    def q_sample_by_id(self, sid: int) -> list[Mapping[str, Any]]:
        s = self.samples.get(int(sid))
        if s is None:
            return []
        return [{**s, "id": Decimal(s["id"])}]

    def q_event_watermark(self) -> list[Mapping[str, Any]]:
        return [{"max_id": max((e["id"] for e in self.events), default=0)}]

    def q_event_for_sample_after(self, after: int, suffix: str) -> list[Mapping[str, Any]]:
        hits = [
            e
            for e in self.events
            if e["id"] > after and any((e.get(c) or "").endswith(suffix) for c in ("object", "uri"))
        ]
        return [{"id": max(e["id"] for e in hits)}] if hits else []

    def q_analysis_snapshot(self, sid: int) -> list[Mapping[str, Any]]:
        return [
            {"id": Decimal(a.id), "status_id": Decimal(a.status_id)}
            for a in self.analyses
            if a.sample_id == sid
        ]

    def q_canceled_analyses(self, sid: int, status: int) -> list[Mapping[str, Any]]:
        return [
            {"id": Decimal(a.id)}
            for a in self.analyses
            if a.sample_id == sid and a.status_id == status
        ]

    def q_pending_samples(self, uuids: list[str], canceled: int) -> list[Mapping[str, Any]]:
        rows: dict[tuple[int, int], dict[str, Any]] = {}
        for a in self.analyses:
            s = self.samples[a.sample_id]
            if s["uuid"] not in uuids or a.status_id == canceled:
                continue
            row = rows.setdefault(
                (a.sample_id, a.type_id),
                {
                    "sample_id": Decimal(a.sample_id),
                    "encounter_uuid": s["uuid"],
                    "sample_type": self.type_names[a.type_id],
                    "accession_number": s["accession_number"],
                    "collection_date": s["collection_date"],
                    "test_names": [],
                },
            )
            row["test_names"].append(self.test_names[a.test_id])
        return list(rows.values())


def _cfg() -> OpenElisConfig:
    return OpenElisConfig(
        base_url=BASE,
        username="lab-user",
        password="lab-pw",
        guard_header_value="guard-key-test",
        ro_dsn="",
        accession_prefix="SM",
    )


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _client(lab: FakeLab, clock: Clock | None = None) -> OpenElisClient:
    clock = clock or Clock()
    return OpenElisClient(_cfg(), sql=lab, sleep=clock.sleep, clock=clock.now)


def _qs(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


def _mock_lab(
    lab: FakeLab,
    *,
    on_collect: Callable[[Mapping[str, str]], None] | None = None,
    before_collect: Callable[[], None] | None = None,
    collect_status: int = 204,
) -> respx.Route:
    respx.post(LOGIN).mock(
        return_value=httpx.Response(
            200, text="ok", headers={"set-cookie": "JSESSIONID=s1; Path=/openelis"}
        )
    )

    def ajax(request: httpx.Request) -> httpx.Response:
        q = _qs(request)
        if q["provider"] == "SampleTypeTestsForSampleProvider":
            lab.calls.append("tests_read")
            return httpx.Response(200, text=lab.sample_tests_xml(int(q["sampleId"])))
        assert q["provider"] == "TestUpdateWithAccessionNumberProvider"
        lab.calls.append("collect")
        if before_collect:
            before_collect()
        (on_collect or lab.apply_collect)(q)
        return httpx.Response(collect_status)

    return respx.get(AJAX).mock(side_effect=ajax)


def _collect_calls(route: respx.Route) -> list[dict[str, str]]:
    return [
        q
        for q in (_qs(c.request) for c in route.calls)
        if q["provider"] == "TestUpdateWithAccessionNumberProvider"
    ]


# ─── pending samples ─────────────────────────────────────────────────────────


async def test_pending_samples_split_by_type():
    lab = FakeLab()
    # One encounter, two sample types -> two lab samples, each with its own id.
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    lab.add_sample(OTHER_SAMPLE_ID, ENC, {9: [31]})
    lab.add_analysis(SAMPLE_ID, 4, 14, status=CANCELED)  # cancelled: not listed
    client = _client(lab)

    rows = await client.pending_samples([ENC])

    assert sorted(rows, key=lambda r: r.sample_id) == [
        PendingSample(
            sample_id=SAMPLE_ID,
            encounter_uuid=ENC,
            sample_type="Blood",
            test_names=("LT-P3-Haemoglobin", "LT-P3-Glucose"),
            accession_number=None,
            collection_date=None,
        ),
        PendingSample(
            sample_id=OTHER_SAMPLE_ID,
            encounter_uuid=ENC,
            sample_type="Urine",
            test_names=("LT-P3-Urine routine",),
            accession_number=None,
            collection_date=None,
        ),
    ]
    assert all(isinstance(r.sample_id, int) for r in rows)


@respx.mock
async def test_collecting_one_type_never_touches_the_other():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    lab.add_sample(OTHER_SAMPLE_ID, ENC, {9: [31]})
    route = _mock_lab(lab)
    client = _client(lab)

    await client.collect(SAMPLE_ID, ACC, DAY)

    sent = _collect_calls(route)
    assert [q["sampleId"] for q in sent] == [str(SAMPLE_ID)]
    assert lab.samples[OTHER_SAMPLE_ID]["accession_number"] is None
    assert all(a.status_id == NOT_TESTED for a in lab.analyses)


async def test_pending_samples_empty_input_does_no_io():
    lab = FakeLab()
    assert await _client(lab).pending_samples([]) == []
    assert lab.calls == []


async def test_pending_samples_refuses_non_uuid_before_io():
    lab = FakeLab()
    for bad in (["not-a-uuid"], ["%"], [ENC + "x"], [123]):
        with pytest.raises(VendorRejected):
            await _client(lab).pending_samples(bad)  # type: ignore[arg-type]
    assert lab.calls == []


# ─── collect: happy path + idempotency + conflict ────────────────────────────


@respx.mock
async def test_collect_records_and_verifies():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13], 9: [31]})
    route = _mock_lab(lab)

    result = await _client(lab).collect(SAMPLE_ID, ACC, DAY)

    assert result == CollectResult(
        sample_id=SAMPLE_ID,
        accession_number=ACC,
        collection_date=DAY,
        event_id=901,
        already_recorded=False,
    )
    sent = _collect_calls(route)
    assert len(sent) == 1
    body = json.loads(sent[0]["typeAndTestIds"])
    assert body == {"tests": {"0": "12,13", "1": "31"}, "types": {"0": "4", "1": "9"}}
    assert sent[0]["collectionDate"] == "25/10/2026"
    # Order matters: read, watermark, snapshot all happen before the send.
    assert lab.calls.index("collect") > lab.calls.index("event_watermark")
    assert lab.calls.index("collect") > lab.calls.index("analysis_snapshot")
    # The snapshot precedes the pre-read, so a test added in between is cancelled
    # from a known state and shows up as newly cancelled.
    assert lab.calls.index("analysis_snapshot") < lab.calls.index("tests_read")


@respx.mock
async def test_collect_idempotent_when_already_ours():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12]}, accession=ACC)
    lab.samples[SAMPLE_ID]["collection_date"] = datetime(2026, 10, 24, 8, 0)
    lab.publish(ENC)
    route = _mock_lab(lab)

    result = await _client(lab).collect(SAMPLE_ID, ACC, DAY)

    assert result.already_recorded is True
    assert result.accession_number == ACC
    assert result.collection_date == date(2026, 10, 24)  # the lab's recorded date
    assert result.event_id == 901
    assert route.call_count == 0  # no read, no collect call
    assert "collect" not in lab.calls


@respx.mock
async def test_collect_conflict_on_other_accession():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12]}, accession="SM2610250099")
    route = _mock_lab(lab)

    with pytest.raises(ConflictError, match="accession_conflict"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert route.call_count == 0


@respx.mock
async def test_collect_unknown_sample_is_not_found():
    lab = FakeLab()
    route = _mock_lab(lab)
    with pytest.raises(NotFound):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert route.call_count == 0


async def test_collect_refuses_foreign_accession_before_io():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12]})
    for bad in ("XY2610250001", "25102026-001", "sm2610250001", "SM261025000111"):
        with pytest.raises(VendorRejected):
            await _client(lab).collect(SAMPLE_ID, bad, DAY)
    assert lab.calls == []


# ─── collect: verification failures ──────────────────────────────────────────


@respx.mock
async def test_204_without_db_accession_raises_not_recorded():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    _mock_lab(lab, on_collect=lambda q: None)  # 204, but nothing recorded
    clock = Clock()

    with pytest.raises(VendorRejected, match="lab_not_recorded"):
        await _client(lab, clock).collect(SAMPLE_ID, ACC, DAY)
    # Polled with the injected sleep, and gave up at about the 10 s budget.
    assert clock.sleeps
    assert 10.0 <= clock.t <= 11.0


@respx.mock
async def test_missing_event_row_raises_event_missing():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})

    def no_event(q: Mapping[str, str]) -> None:
        lab.apply_collect(q)
        lab.events.clear()

    _mock_lab(lab, on_collect=no_event)
    with pytest.raises(VendorRejected, match="lab_event_missing"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_event_row_of_other_sample_does_not_verify():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    lab.add_sample(OTHER_SAMPLE_ID, OTHER_ENC, {9: [31]})

    def other_event(q: Mapping[str, str]) -> None:
        lab.apply_collect(q)
        lab.events.clear()
        lab.publish(OTHER_ENC)  # a new event after the watermark, for another sample

    _mock_lab(lab, on_collect=other_event)
    with pytest.raises(VendorRejected, match="lab_event_missing"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_collect_detects_cancelled_test(caplog: pytest.LogCaptureFixture):
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})

    def cancels_one(q: Mapping[str, str]) -> None:
        lab.apply_collect(q)
        lab.analyses[1].status_id = CANCELED  # a test from the pre-read is gone

    _mock_lab(lab, on_collect=cancels_one)
    with (
        caplog.at_level(logging.ERROR, logger=openelis.__name__),
        pytest.raises(VendorRejected, match="lab_tests_changed"),
    ):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert any(r.levelno == logging.ERROR and "7001" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_collect_detects_shrink_without_a_cancelled_row():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})

    def drops_one(q: Mapping[str, str]) -> None:
        lab.apply_collect(q)
        del lab.analyses[1]  # gone from the post-read, but no cancelled row to find

    _mock_lab(lab, on_collect=drops_one)
    with pytest.raises(VendorRejected, match="lab_tests_changed"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_collect_detects_test_added_after_pre_read(caplog: pytest.LogCaptureFixture):
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    added: list[Analysis] = []
    # The lab feed adds a test after our pre-read; our send omits it; the lab
    # system cancels it. post == pre, so only the cancelled-analysis query sees it.
    _mock_lab(lab, before_collect=lambda: added.append(lab.add_analysis(SAMPLE_ID, 4, 14)))

    with (
        caplog.at_level(logging.ERROR, logger=openelis.__name__),
        pytest.raises(VendorRejected, match="lab_tests_changed"),
    ):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert added[0].status_id == CANCELED
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(str(added[0].id) in m for m in errors)
    # ids only, never test names
    assert not any("LT-P3-" in m for m in errors)


@respx.mock
async def test_previously_cancelled_test_is_not_a_hit():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    lab.add_analysis(SAMPLE_ID, 4, 14, status=CANCELED)  # cancelled before we started
    _mock_lab(lab)

    result = await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert result.already_recorded is False


@respx.mock
async def test_unconfirmed_send_is_verified_in_the_db():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    respx.post(LOGIN).mock(
        return_value=httpx.Response(
            200, text="ok", headers={"set-cookie": "JSESSIONID=s1; Path=/openelis"}
        )
    )

    def ajax(request: httpx.Request) -> httpx.Response:
        q = _qs(request)
        if q["provider"] == "SampleTypeTestsForSampleProvider":
            return httpx.Response(200, text=lab.sample_tests_xml(SAMPLE_ID))
        lab.apply_collect(q)  # the lab recorded it, but the answer was lost
        raise httpx.ReadTimeout("lost", request=request)

    respx.get(AJAX).mock(side_effect=ajax)
    result = await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert result.accession_number == ACC and result.already_recorded is False


@respx.mock
async def test_unconfirmed_send_not_in_db_stays_rejected():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    respx.post(LOGIN).mock(
        return_value=httpx.Response(
            200, text="ok", headers={"set-cookie": "JSESSIONID=s1; Path=/openelis"}
        )
    )

    def ajax(request: httpx.Request) -> httpx.Response:
        q = _qs(request)
        if q["provider"] == "SampleTypeTestsForSampleProvider":
            return httpx.Response(200, text=lab.sample_tests_xml(SAMPLE_ID))
        raise httpx.ReadTimeout("lost", request=request)

    respx.get(AJAX).mock(side_effect=ajax)
    with pytest.raises(VendorRejected, match="lab_not_recorded"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_non_204_is_not_verified_and_stays_rejected():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    _mock_lab(lab, on_collect=lambda q: None, collect_status=200)
    with pytest.raises(VendorRejected, match="lab_collect_http_200"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_gateway_error_after_the_lab_ran_is_verified():
    # A 5xx can arrive after the lab already applied the call (proxy timeout,
    # error after commit). The DB is checked, so the cancelled test is reported
    # now; otherwise a retry would take the idempotent path and never see it.
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    added: list[Analysis] = []
    _mock_lab(
        lab,
        before_collect=lambda: added.append(lab.add_analysis(SAMPLE_ID, 4, 14)),
        collect_status=504,
    )
    with pytest.raises(VendorRejected, match="lab_tests_changed"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert added[0].status_id == CANCELED


@respx.mock
async def test_gateway_error_after_the_lab_ran_cleanly_is_success():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    _mock_lab(lab, collect_status=502)
    result = await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert result.already_recorded is False and result.event_id == 901


@respx.mock
async def test_gateway_error_with_nothing_applied_is_not_recorded():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    _mock_lab(lab, on_collect=lambda q: None, collect_status=504)
    clock = Clock()
    with pytest.raises(VendorRejected, match="lab_not_recorded"):
        await _client(lab, clock).collect(SAMPLE_ID, ACC, DAY)
    assert 10.0 <= clock.t <= 11.0


class FlakyAfterSendLab(FakeLab):
    """The lab DB becomes unreachable once the collect call has been sent."""

    def q_sample_by_id(self, sid: int) -> list[Mapping[str, Any]]:
        if "collect" in self.calls:
            raise TransientError("openelis db: connection error: ConnectionResetError")
        return super().q_sample_by_id(sid)


@respx.mock
async def test_db_failure_after_the_send_is_unverified_not_transient():
    # After the send the outcome is unknown; a plain TransientError would invite
    # a blind retry that takes the idempotent path and skips every check.
    lab = FlakyAfterSendLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    _mock_lab(lab)
    with pytest.raises(VendorRejected, match="lab_collect_unverified"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_idempotent_path_without_an_event_is_event_missing():
    # Accession already ours but no accession event: OpenMRS never heard of it,
    # so a retry must not turn the earlier failure into a clean success.
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12]}, accession=ACC)
    route = _mock_lab(lab)
    with pytest.raises(VendorRejected, match="lab_event_missing"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)
    assert route.call_count == 0


@respx.mock
async def test_earlier_event_of_a_sibling_sample_does_not_verify():
    # Two sample types of one encounter share its uuid, hence the event suffix.
    # The sibling's event predates our send; only events after the watermark count.
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
    lab.add_sample(OTHER_SAMPLE_ID, ENC, {9: [31]}, accession="SM2610250002")
    lab.publish(ENC)

    def no_new_event(q: Mapping[str, str]) -> None:
        lab.apply_collect(q)
        lab.events.pop()  # the collect published nothing

    _mock_lab(lab, on_collect=no_new_event)
    with pytest.raises(VendorRejected, match="lab_event_missing"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_event_must_be_an_accession_event():
    # An event whose content ends in the uuid under another resource path does
    # not verify the collection.
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})

    def other_resource(q: Mapping[str, str]) -> None:
        lab.apply_collect(q)
        lab.events[-1]["object"] = f"/openelis/ws/rest/encounter/{ENC}"

    _mock_lab(lab, on_collect=other_resource)
    with pytest.raises(VendorRejected, match="lab_event_missing"):
        await _client(lab).collect(SAMPLE_ID, ACC, DAY)


@respx.mock
async def test_collect_refuses_sample_without_encounter_uuid():
    for bad in (None, "", "not-a-uuid"):
        lab = FakeLab()
        lab.add_sample(SAMPLE_ID, ENC, {4: [12, 13]})
        lab.samples[SAMPLE_ID]["uuid"] = bad
        route = _mock_lab(lab)
        with pytest.raises(VendorRejected, match="no encounter uuid"):
            await _client(lab).collect(SAMPLE_ID, ACC, DAY)
        assert route.call_count == 0


@respx.mock
async def test_canceled_status_must_resolve_to_exactly_one_row():
    for statuses in ([], [{"id": 1}, {"id": 2}]):
        lab = FakeLab(statuses=statuses)
        lab.add_sample(SAMPLE_ID, ENC, {4: [12]})
        route = _mock_lab(lab)
        with pytest.raises(ConfigError):
            await _client(lab).collect(SAMPLE_ID, ACC, DAY)
        assert not _collect_calls(route)


@respx.mock
async def test_canceled_status_resolved_once_per_client():
    lab = FakeLab()
    lab.add_sample(SAMPLE_ID, ENC, {4: [12]})
    lab.add_sample(OTHER_SAMPLE_ID, OTHER_ENC, {9: [31]})
    _mock_lab(lab)
    client = _client(lab)
    await client.collect(SAMPLE_ID, ACC, DAY)
    await client.collect(OTHER_SAMPLE_ID, "SM2610250002", DAY)
    await client.pending_samples([ENC])
    assert lab.calls.count("canceled_status_id") == 1


# ─── SQL transport ───────────────────────────────────────────────────────────


def test_canceled_status_query_in_allowlist():
    for name in (
        "canceled_status_id",
        "canceled_analyses",
        "analysis_snapshot",
        "event_for_sample_after",
        "event_watermark",
        "sample_by_id",
        "pending_samples",
    ):
        assert name in LAB_QUERIES
    assert "'Test Canceled'" in LAB_QUERIES["canceled_status_id"]
    assert "'ANALYSIS'" in LAB_QUERIES["canceled_status_id"]
    # The fake implements the status filters itself, so pin them in the text.
    assert "a.status_id <> $2::bigint" in LAB_QUERIES["pending_samples"]
    assert "a.status_id = $2::bigint" in LAB_QUERIES["canceled_analyses"]
    assert "e.id > $1::bigint" in LAB_QUERIES["event_for_sample_after"]
    # Every fixed query is a read; parameters only, no string building.
    for sql in LAB_QUERIES.values():
        assert sql.lstrip().upper().startswith("SELECT")
        for word in ("INSERT", "UPDATE ", "DELETE", "DROP", "ALTER", "LIKE", "{", "%s"):
            assert word not in sql.upper()


async def test_sql_transport_refuses_unknown_query():
    lab = FakeLab()
    sql = OpenElisSql(lab)
    for bad in ("drop_everything", "SELECT 1", LAB_QUERIES["sample_by_id"]):
        with pytest.raises(ConfigError):
            await sql.run(bad, 1)
    assert lab.calls == []


class FakeConn:
    def __init__(self, timeout: str) -> None:
        self.timeout = timeout
        self.fetched: list[str] = []
        self.readonly: list[bool] = []
        self.closed = False

    async def fetchval(self, sql: str) -> str:
        assert sql == "SHOW statement_timeout"
        return self.timeout

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched.append(sql)
        return [{"max_id": 3}]

    def transaction(self, *, readonly: bool = False) -> Any:
        conn = self

        class Tx:
            async def __aenter__(self) -> None:
                conn.readonly.append(readonly)

            async def __aexit__(self, *exc: object) -> None:
                return None

        return Tx()

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


def _executor(timeout: str) -> tuple[AsyncpgLabExecutor, list[FakeConn]]:
    conns: list[FakeConn] = []

    async def connect(dsn: str, *, timeout: float) -> FakeConn:
        conns.append(FakeConn(tmo))
        return conns[-1]

    tmo = timeout
    return AsyncpgLabExecutor(DSN, connect=connect), conns


async def test_executor_runs_fixed_queries_read_only():
    ex, conns = _executor("5s")
    rows = await ex.fetch(LAB_QUERIES["event_watermark"])
    assert rows == [{"max_id": 3}]
    assert conns[0].readonly == [True]
    await ex.aclose()
    assert conns[0].closed


async def test_executor_refuses_sql_outside_the_allowlist():
    ex, conns = _executor("5s")
    with pytest.raises(ConfigError):
        await ex.fetch("SELECT * FROM clinlims.sample")
    assert conns == []


@pytest.mark.parametrize("value", ["0", "6s", "1min", "10000ms"])
async def test_executor_refuses_missing_or_long_statement_timeout(value: str):
    ex, conns = _executor(value)
    with pytest.raises(ConfigError, match="statement_timeout"):
        await ex.fetch(LAB_QUERIES["event_watermark"])
    assert conns[0].closed and conns[0].fetched == []


def test_parse_pg_duration():
    assert parse_pg_duration("5s") == 5.0
    assert parse_pg_duration("5000ms") == 5.0
    assert parse_pg_duration("2500") == 2.5  # bare number is milliseconds
    assert parse_pg_duration("1min") == 60.0
    assert parse_pg_duration("0") == 0.0
    with pytest.raises(ConfigError):
        parse_pg_duration("soon")


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "postgresql://lab-db.example.test/labdb",  # no user/password: libpq env fallback
        "postgresql://u@lab-db.example.test/labdb",  # no password
        "postgresql://u:p@/labdb",  # no host
        "postgresql://u:p@lab-db.example.test",  # no database
        "mysql://u:p@lab-db.example.test/labdb",
        "postgresql://u:p@lab-db.example.test/labdb?passfile=/x",
    ],
)
def test_executor_refuses_incomplete_dsn(dsn: str):
    with pytest.raises(ConfigError):
        AsyncpgLabExecutor(dsn)


async def test_client_without_dsn_raises_config_error_before_io():
    client = OpenElisClient(_cfg())
    with pytest.raises(ConfigError):
        await client.pending_samples([ENC])
    with pytest.raises(ConfigError):
        await client.collect(SAMPLE_ID, ACC, DAY)
    await client.aclose()


def test_config_error_never_echoes_the_dsn_password():
    with pytest.raises(ConfigError) as exc:
        AsyncpgLabExecutor("postgresql://u:secret-pw-test@lab-db.example.test/labdb?x=1")
    assert "secret-pw-test" not in str(exc.value)


def test_asyncpg_not_imported_at_module_import():
    code = (
        "import sys\n"
        "import sm_common.integrations.adapters.openelis as m\n"
        "m.OpenElisClient(m.OpenElisConfig(base_url='https://lab.example.test', "
        "username='u', password='p', guard_header_value='g', "
        "ro_dsn='postgresql://u:p@lab-db.example.test/labdb', accession_prefix='SM'))\n"
        "assert 'asyncpg' not in sys.modules, 'asyncpg imported eagerly'\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


async def test_executor_drops_a_broken_connection_and_reconnects():
    conns: list[FakeConn] = []

    class BreaksOnce(FakeConn):
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            if len(conns) == 1:
                raise ConnectionResetError("reset")
            return await super().fetch(sql, *args)

    async def connect(dsn: str, *, timeout: float) -> FakeConn:
        conns.append(BreaksOnce("5s"))
        return conns[-1]

    ex = AsyncpgLabExecutor(DSN, connect=connect)
    with pytest.raises(TransientError):
        await ex.fetch(LAB_QUERIES["event_watermark"])
    assert conns[0].closed
    assert await ex.fetch(LAB_QUERIES["event_watermark"]) == [{"max_id": 3}]
    assert len(conns) == 2


async def test_executor_does_not_turn_a_programming_error_into_transient():
    class Broken(FakeConn):
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            raise KeyError("renamed_column")

    async def connect(dsn: str, *, timeout: float) -> FakeConn:
        return Broken("5s")

    ex = AsyncpgLabExecutor(DSN, connect=connect)
    with pytest.raises(KeyError):
        await ex.fetch(LAB_QUERIES["event_watermark"])


async def test_executor_maps_connection_failure_to_transient():
    async def refuse(dsn: str, *, timeout: float) -> Any:
        raise ConnectionRefusedError("refused")

    ex = AsyncpgLabExecutor(DSN, connect=refuse)
    with pytest.raises(TransientError) as exc:
        await ex.fetch(LAB_QUERIES["event_watermark"])
    assert "pw-test" not in str(exc.value)
