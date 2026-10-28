"""Offline checks of the Phase 3 live harness groups (``--orders``, ``--lab``).

The live run needs a box; these prove, without one, that:
- every CHECK row is registered with its topic, in the right group;
- the collect probe goes through ``OpenElisClient.collect`` and so sends the FULL test
  list read just before (never a list the harness builds);
- the added-test probe injects its test between the pre-read and the send;
- the guard probe never names the write provider and never sends the guard header;
- lab config comes from env only, and the library reads no env at all.
All fixtures are synthetic.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.openelis import (
    GUARD_HEADER,
    LAB_QUERIES,
    PROVIDER_COLLECT,
    PROVIDER_SAMPLE_TESTS,
    OpenElisClient,
    OpenElisConfig,
    PendingSample,
)
from sm_common.integrations.record_types import (
    RecordEncounterInput,
    RecordEncounterResult,
    RecordVisit,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness" / "bahmni"
sys.path.insert(0, str(HARNESS))
import phase3_checks  # noqa: E402

# THE list: every CHECK line the --orders and --lab groups print, with the topic it verifies.
# Change it only together with harness/bahmni/phase3_checks.py and its README.
CHECK_TOPIC = {
    "noncoded_drug_not_in_odoo": "noncoded_drug_no_sale_line",
    "visit_orders_read": "visit_order_read",
    "fulfiller_details_write": "fulfiller_status_mirror",
    "medicationdispense_absent": "fulfiller_status_mirror",
    "lab_tests_catalogue": "lab_test_catalogue",
    "lab_order_to_sample": "lab_order_sample",
    "collect_verified": "sample_collect",
    "sibling_orders_marked": "sample_collect",
    "accession_len": "sample_collect",
    "event_record_columns": "sample_collect",
    "guard_header_required": "lab_write_guard",
    "duplicate_accession": "duplicate_accession",
    "full_list_collect_cancels_nothing": "collect_keeps_orders",
    "added_test_not_cancelled": "collect_keeps_orders",
    "elis_login_path": "lab_login_path",
}
# The topics each group covers.
ORDERS_TOPICS = {
    "noncoded_drug_no_sale_line",
    "visit_order_read",
    "fulfiller_status_mirror",
    "lab_test_catalogue",
}
LAB_TOPICS = {
    "lab_order_sample",
    "sample_collect",
    "lab_write_guard",
    "duplicate_accession",
    "collect_keeps_orders",
    "lab_login_path",
}

ELIS_ENV = {
    "ELIS_BASE_URL": "https://lab.example.test",
    "ELIS_USER": "lt-p3-user",
    "ELIS_PASS": "lt-p3-pass",
    "ELIS_GUARD_KEY": "lt-p3-guard",
    "ELIS_RO_DSN": "postgresql://lt_ro:lt-pw@db.example.test:5432/labdb",
    "ELIS_ACCESSION_PREFIX": "SM",
}
UUID_LITERAL = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\b(?=[A-Za-z]*\d)[0-9A-Za-z]{32,38}\b"
)
ENC = "0b7d1c1e-4d8a-4f7e-9a51-3b2a6c9d0e11"  # synthetic
SAMPLE_ID = 4711
CANCELED = 9


# ─── registry ────────────────────────────────────────────────────────────────


def test_checks_map_to_topics() -> None:
    groups = {"orders": phase3_checks.ORDERS_CHECKS, "lab": phase3_checks.LAB_CHECKS}
    names = [n for rows in groups.values() for n, _topic, _fn in rows]
    assert len(names) == len(set(names)), "a CHECK is registered twice"
    registered = {n: topic for rows in groups.values() for n, topic, _fn in rows}
    missing = sorted(set(CHECK_TOPIC) - set(registered))
    assert not missing, f"orders/lab groups lack CHECKs: {missing}"
    extra = sorted(set(registered) - set(CHECK_TOPIC))
    assert not extra, f"orders/lab groups register CHECKs CHECK_TOPIC does not name: {extra}"
    wrong = {n: registered[n] for n, topic in CHECK_TOPIC.items() if registered[n] != topic}
    assert not wrong, f"CHECK -> topic mapping differs from CHECK_TOPIC: {wrong}"
    assert {topic for _n, topic, _f in groups["orders"]} == ORDERS_TOPICS
    assert {topic for _n, topic, _f in groups["lab"]} == LAB_TOPICS
    verify = (HARNESS / "verify_adapter_live.py").read_text()
    assert '"--orders"' in verify and '"--lab"' in verify
    assert '"--phase3"' in (HARNESS / "seed_data.py").read_text()


# ─── fakes ───────────────────────────────────────────────────────────────────

SAMPLE_TESTS_XML = (
    "<fieldmessage><formfield><samples>"
    "<sample><sampleType>1</sampleType><test>11</test><test>12</test></sample>"
    "<sample><sampleType>2</sampleType><test>21</test></sample>"
    "</samples></formfield></fieldmessage>"
)
FULL_LIST = {"tests": {"0": "11,12", "1": "21"}, "types": {"0": "1", "1": "2"}}


class FakeLab:
    """The lab system: its servlet (MockTransport) and its read-only DB (executor)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.events: list[str] = []
        self.accession: str | None = None
        self.analyses: dict[int, int] = {101: 1, 102: 1, 103: 1}
        self.added: int | None = None  # an analysis the feed added after the pre-read
        self.records = True  # False: the send answers 204 but records nothing
        self.send_status = 204
        self.pending_ids = [SAMPLE_ID]  # the samples pending_samples lists

    async def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path.endswith("/ValidateLogin.do"):
            return httpx.Response(302, headers={"location": "/openelis/Dashboard.do",
                                                "set-cookie": "JSESSIONID=lt"})
        q = dict(req.url.params)
        if q.get("provider") == PROVIDER_SAMPLE_TESTS:
            self.events.append("pre_read" if "pre_read" not in self.events else "post_read")
            return httpx.Response(200, text=SAMPLE_TESTS_XML)
        if q.get("provider") == PROVIDER_COLLECT:
            self.events.append("send")
            if self.send_status != 204:
                return httpx.Response(self.send_status)
            if self.records:
                self.accession = q["accessionNumber"]
            if self.added is not None:
                # The lab system cancels every test missing from the list.
                self.analyses[self.added] = CANCELED
            return httpx.Response(204)
        return httpx.Response(404)

    async def fetch(self, sql: str, *args: Any) -> Sequence[Mapping[str, Any]]:
        name = next(k for k, v in LAB_QUERIES.items() if v == sql)
        if name == "canceled_status_id":
            return [{"id": CANCELED}]
        if name == "sample_by_id":
            return [{"id": SAMPLE_ID, "uuid": ENC, "accession_number": self.accession,
                     "collection_date": date(2026, 10, 25) if self.accession else None}]
        if name == "event_watermark":
            return [{"max_id": 50}]
        if name == "event_for_sample_after":
            return [{"id": 51}] if self.accession and args[0] < 51 else []
        if name == "analysis_snapshot":
            return [{"id": a, "status_id": s} for a, s in self.analyses.items()]
        if name == "canceled_analyses":
            return [{"id": a} for a, s in self.analyses.items() if s == args[1]]
        if name == "pending_samples":
            return [{"sample_id": sid, "encounter_uuid": ENC, "sample_type": "Blood",
                     "accession_number": self.accession, "collection_date": None,
                     "test_names": ["LT-P3 Test A"]} for sid in self.pending_ids]
        raise AssertionError(f"unexpected query {name}")

    async def aclose(self) -> None:
        return None


def _bahmni_http() -> BahmniHttp:
    async def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    return BahmniHttp(
        "https://bahmni.example.test", "u", "p", transport=httpx.MockTransport(handle)
    )


def _ctx(
    lab: FakeLab, guard: httpx.MockTransport | None = None, odoo: Any = None
) -> phase3_checks.OrdersLabCtx:
    cfg = {"harness": {"provider_uuid": "", "tag": "LT-ADAPTER"}, "field_mapping": {}}
    config = BahmniConfig(
        location_uuid="loc", identifier_type_uuid="idt", identifier_source_uuid="ids"
    )
    elis_cfg = OpenElisConfig(
        base_url="https://lab.example.test",
        username="lt-p3-user",
        password="lt-p3-pass",
        guard_header_value="lt-p3-guard",
        ro_dsn="",
        accession_prefix="SM",
    )

    def make(cls: type[OpenElisClient]) -> OpenElisClient:
        async def no_sleep(_s: float) -> None:
            return None

        return cls(
            elis_cfg,
            transport=httpx.MockTransport(lab.handle),
            sql=lab,
            sleep=no_sleep,
            verify_timeout_s=0.0,
        )

    async def no_wait(_s: float) -> None:
        return None

    extra: dict[str, Any] = {"odoo_proxy": odoo} if odoo is not None else {}
    return phase3_checks.OrdersLabCtx(
        cfg,
        _bahmni_http(),
        config,
        elis_factory=make,
        guard_transport=guard,
        sleep=no_wait,
        **extra,
    )


def _sent_lists(lab: FakeLab) -> list[dict[str, Any]]:
    return [
        json.loads(dict(r.url.params)["typeAndTestIds"])
        for r in lab.requests
        if dict(r.url.params).get("provider") == PROVIDER_COLLECT
    ]


def _pending() -> PendingSample:
    return PendingSample(
        sample_id=SAMPLE_ID,
        encounter_uuid=ENC,
        sample_type="Blood",
        test_names=("LT-P3 Test A", "LT-P3 Test B", "LT-P3 Test C"),
        accession_number=None,
        collection_date=None,
    )


# ─── the collect probes ──────────────────────────────────────────────────────


async def test_collect_probe_sends_full_test_list() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["sample"] = _pending()
    status, detail = await phase3_checks.collect_verified(x)
    assert status == "PASS", detail
    # Exactly one send, and it carried every test of every type read just before it.
    assert _sent_lists(lab) == [FULL_LIST]
    assert lab.events[:2] == ["pre_read", "send"]
    assert x.state["collect"].accession_number == lab.accession
    # The harness itself never builds the destructive call: only the client does.
    src = (HARNESS / "phase3_checks.py").read_text()
    assert PROVIDER_COLLECT not in src and "typeAndTestIds" not in src
    assert "collect_params" not in src and "provider_send_once" not in src


async def test_added_test_probe_injects_between_read_and_send() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["added_sample"] = _pending()

    async def add_test(sample: PendingSample) -> bool:
        assert sample.sample_id == SAMPLE_ID
        assert lab.events == ["pre_read"], "the test must be added after the pre-read"
        lab.events.append("inject")
        lab.added = 104
        lab.analyses[104] = 1
        return True

    x.add_test_to_sample = add_test  # type: ignore[method-assign]
    status, detail = await phase3_checks.added_test_not_cancelled(x)
    assert lab.events[:3] == ["pre_read", "inject", "send"]
    assert status == "PASS", detail
    assert "lab_tests_changed" in detail
    # The stale list was sent unchanged: the probe never patches the list itself.
    assert _sent_lists(lab) == [FULL_LIST]


async def test_added_test_probe_fails_when_the_client_misses_the_cancel() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["added_sample"] = _pending()

    async def add_nothing(_s: PendingSample) -> bool:
        return True  # claims success, but the lab cancels nothing: collect succeeds

    x.add_test_to_sample = add_nothing  # type: ignore[method-assign]
    status, _detail = await phase3_checks.added_test_not_cancelled(x)
    assert status == "FAIL"


async def test_added_test_probe_inconclusive_and_unsent_when_the_test_never_lands() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["added_sample"] = _pending()

    async def never_lands(_s: PendingSample) -> bool:
        return False

    x.add_test_to_sample = never_lands  # type: ignore[method-assign]
    status, _detail = await phase3_checks.added_test_not_cancelled(x)
    assert status == "INCONCLUSIVE"
    assert _sent_lists(lab) == [], "nothing may be sent when the injection did not land"


# ─── the guard probe ─────────────────────────────────────────────────────────


def _guard(statuses: dict[str, int], seen: list[httpx.Request]) -> httpx.MockTransport:
    async def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(statuses.get(req.method, 403))

    return httpx.MockTransport(handle)


async def test_guard_probe_never_names_the_write_provider() -> None:
    lab = FakeLab()
    seen: list[httpx.Request] = []
    x = _ctx(lab, guard=_guard({}, seen))
    x.state["sample"] = _pending()
    status, detail = await phase3_checks.guard_header_required(x)
    assert status == "PASS", detail
    assert seen, "the guard probe sent nothing without the header"
    assert {r.method for r in seen} >= {"GET", "HEAD"}
    for r in seen:
        assert GUARD_HEADER.lower() not in {k.lower() for k in r.headers}
        raw = unquote(str(r.url)).casefold()
        assert PROVIDER_COLLECT.casefold() not in raw
        assert "accessionnumber" not in raw
    # Every variant is sent: plain GET and HEAD, an upper-cased and a percent-encoded
    # parameter name, and a lower-cased provider value.
    sent = [(r.method, r.url.query.decode()) for r in seen]
    read = f"{PROVIDER_SAMPLE_TESTS}&sampleId=0"
    assert ("GET", f"provider={read}") in sent
    assert ("HEAD", f"provider={read}") in sent
    assert ("GET", f"PROVIDER={read}") in sent
    assert ("GET", f"%70rovider={read}") in sent
    assert ("GET", f"provider={PROVIDER_SAMPLE_TESTS.lower()}&sampleId=0") in sent
    assert len(sent) == 5
    # With the header (through the client), the read provider answers.
    assert any(
        GUARD_HEADER in r.headers and dict(r.url.params).get("provider") == PROVIDER_SAMPLE_TESTS
        for r in lab.requests
    )


async def test_guard_probe_fails_when_a_headerless_request_gets_through() -> None:
    lab = FakeLab()
    seen: list[httpx.Request] = []
    x = _ctx(lab, guard=_guard({"HEAD": 200}, seen))
    x.state["sample"] = _pending()
    status, _detail = await phase3_checks.guard_header_required(x)
    assert status == "FAIL"


async def test_guard_probes_without_header_even_when_no_sample_appeared() -> None:
    # lab_order_to_sample produced no sample: the header-less probes (sampleId=0) still
    # run, and a header-less 200 is a FAIL, not "blocked by an earlier check".
    lab = FakeLab()
    seen: list[httpx.Request] = []
    x = _ctx(lab, guard=_guard({"HEAD": 200}, seen))
    status, detail = await phase3_checks.guard_header_required(x)
    assert status == "FAIL", detail
    assert len(seen) == 5


async def test_guard_without_sample_reports_probe_statuses_but_not_pass() -> None:
    lab = FakeLab()
    seen: list[httpx.Request] = []
    x = _ctx(lab, guard=_guard({}, seen))
    status, detail = await phase3_checks.guard_header_required(x)
    assert status == "INCONCLUSIVE", detail
    assert "GET:403" in detail and "HEAD:403" in detail
    assert len(seen) == 5


# ─── collect outcomes ────────────────────────────────────────────────────────


async def test_collect_verified_fails_when_already_recorded() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["sample"] = _pending()
    lab.accession = x.accession(1)  # the sample already carries this run's accession
    status, detail = await phase3_checks.collect_verified(x)
    assert status == "FAIL", detail
    assert _sent_lists(lab) == []
    assert "already_recorded=True" in detail
    assert "sent=204" not in detail and "event_after_watermark=True" not in detail


async def test_added_test_probe_fails_on_another_rejection_code() -> None:
    lab = FakeLab()
    lab.records = False  # the send is answered but nothing is recorded
    x = _ctx(lab)
    x.state["added_sample"] = _pending()

    async def add_test(_s: PendingSample) -> bool:
        return True

    x.add_test_to_sample = add_test  # type: ignore[method-assign]
    status, detail = await phase3_checks.added_test_not_cancelled(x)
    assert status == "FAIL", detail
    assert "lab_not_recorded" in detail


async def test_duplicate_accession_falls_back_to_the_added_sample() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["sample"] = _pending()
    status, detail = await phase3_checks.collect_verified(x)
    assert status == "PASS", detail
    first = x.state["collect"].accession_number
    # One sample type in the catalogue: no sibling, but the added-test patient's sample.
    other = 4712
    added = _pending()
    x.state["added_sample"] = PendingSample(
        sample_id=other,
        encounter_uuid=added.encounter_uuid,
        sample_type=added.sample_type,
        test_names=added.test_names,
        accession_number=None,
        collection_date=None,
    )
    lab.pending_ids = [other]
    lab.accession = None
    lab.send_status = 400
    status, detail = await phase3_checks.duplicate_accession(x)
    assert status == "PASS", detail
    assert "refused" in detail and "lab_collect_http_400" in detail
    dup_sends = [
        dict(r.url.params)
        for r in lab.requests
        if dict(r.url.params).get("provider") == PROVIDER_COLLECT
    ][1:]
    assert [(d["sampleId"], d["accessionNumber"]) for d in dup_sends] == [(str(other), first)]


async def test_duplicate_accession_skips_an_added_sample_already_collected() -> None:
    lab = FakeLab()
    x = _ctx(lab)
    x.state["sample"] = _pending()
    status, detail = await phase3_checks.collect_verified(x)
    assert status == "PASS", detail
    x.state["added_sample"] = _pending()  # the added-test probe collected it since
    sends = len(_sent_lists(lab))
    status, detail = await phase3_checks.duplicate_accession(x)
    assert status == "INCONCLUSIVE", detail
    assert len(_sent_lists(lab)) == sends


# ─── harness-only SQL ────────────────────────────────────────────────────────


class _StubConn:
    def __init__(self, timeout: str) -> None:
        self.timeout = timeout
        self.closed = False
        self.fetched: list[str] = []

    async def fetchval(self, _sql: str) -> str:
        return self.timeout

    def transaction(self, readonly: bool) -> Any:
        assert readonly

        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_a: Any) -> None:
                return None

        return _Tx()

    async def fetch(self, sql: str, *_args: Any) -> list[dict[str, Any]]:
        self.fetched.append(sql)
        return [{"id": 51}]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("timeout", "ok"), [("30s", False), ("0", False), ("5s", True)])
async def test_harness_sql_needs_a_short_statement_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: str, ok: bool
) -> None:
    conn = _StubConn(timeout)

    async def connect(_dsn: str, timeout: float) -> _StubConn:
        return conn

    monkeypatch.setitem(sys.modules, "asyncpg", type(sys)("asyncpg"))
    sys.modules["asyncpg"].connect = connect  # type: ignore[attr-defined]
    sql = phase3_checks.HarnessSql(ELIS_ENV["ELIS_RO_DSN"])
    if ok:
        assert await sql.fetch("event_row", 51) == [{"id": 51}]
        assert conn.fetched == [phase3_checks.HARNESS_SQL["event_row"]]
    else:
        with pytest.raises(RuntimeError):
            await sql.fetch("event_row", 51)
        assert conn.closed and conn.fetched == []


# ─── the Odoo read (noncoded_drug_no_sale_line) ──────────────────────────────

DRUG_ID = "LT-P3-ID0001"  # synthetic patient identifiers
ORDERS_ID = "LT-P3-ID0002"


class FakeOdoo:
    """XML-RPC endpoints over a tiny ``sale.order.line`` table (synthetic rows)."""

    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self.lines = lines
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, url: str, **_kw: Any) -> FakeOdoo:
        return self

    def authenticate(self, *_a: Any) -> int:
        return 7

    @staticmethod
    def _get(row: Mapping[str, Any], path: str) -> Any:
        v: Any = row
        for part in path.split("."):
            v = v.get(part) if isinstance(v, Mapping) else None
        return v

    def _leaf(self, row: Mapping[str, Any], leaf: Sequence[Any]) -> bool:
        field, op, value = leaf
        got = self._get(row, field)
        if op == "=":
            return bool(got == value)
        if op == "ilike":
            return isinstance(got, str) and value.casefold() in got.casefold()
        raise AssertionError(f"unexpected operator {op}")

    def _match(self, row: Mapping[str, Any], domain: Sequence[Any]) -> bool:
        stack: list[bool] = []
        for term in reversed(domain):
            if term == "|":
                a, b = stack.pop(), stack.pop()
                stack.append(a or b)
            elif term == "&":
                a, b = stack.pop(), stack.pop()
                stack.append(a and b)
            else:
                stack.append(self._leaf(row, term))
        return all(stack)

    def execute_kw(
        self, _db: str, _uid: int, _pw: str, model: str, method: str, args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((model, method, args))
        assert model == "sale.order.line"
        hits = [r for r in self.lines if self._match(r, args[0])]
        if method == "search_count":
            return len(hits)
        if method == "search_read":
            return [{"id": i, "name": r["name"], "product_id": r["product_id"]}
                    for i, r in enumerate(hits)]
        raise AssertionError(f"unexpected method {method}")


def _odoo_line(product: str, ref: str, partner: str) -> dict[str, Any]:
    # The line is named after the product it mapped to, never our free-text drug name.
    return {"name": product, "product_id": [3, product],
            "order_id": {"partner_id": {"ref": ref, "name": partner}}}


def _orders_state(x: phase3_checks.OrdersLabCtx) -> None:
    x.state["orders"] = {
        "patient": "p", "visit": "v", "encounter": "e", "labs": ["lab"],
        "drug_identifier": DRUG_ID, "written_at": -1e9,
    }


@pytest.fixture
def odoo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    for k in phase3_checks.ODOO_ENV:
        monkeypatch.setenv(k, "lt-p3-odoo")
    monkeypatch.setenv("HARNESS_SAMPLES_DIR", str(tmp_path))
    return tmp_path


async def test_noncoded_drug_line_found_by_patient_not_by_line_name(
    odoo_env: pathlib.Path,
) -> None:
    name = phase3_checks.NAME
    odoo = FakeOdoo([
        _odoo_line("LT-P3 Mapped Product", DRUG_ID, f"{name} Drug {name}"),
        _odoo_line("LT-P3 Lab Test", ORDERS_ID, f"{name} Orders {name}"),
    ])
    x = _ctx(FakeLab(), odoo=odoo)
    _orders_state(x)
    status, detail = await phase3_checks.noncoded_drug_not_in_odoo(x)
    assert status == "FAIL", detail
    assert "sale_order_lines=1" in detail
    saved = "".join(f.read_text() for f in odoo_env.glob("*.json"))
    assert "LT-P3 Mapped Product" in saved and "LT-P3 Lab Test" not in saved


async def test_noncoded_drug_ignores_the_orders_patients_lab_line(
    odoo_env: pathlib.Path,
) -> None:
    name = phase3_checks.NAME
    odoo = FakeOdoo([_odoo_line("LT-P3 Lab Test", ORDERS_ID, f"{name} Orders {name}")])
    x = _ctx(FakeLab(), odoo=odoo)
    _orders_state(x)
    status, detail = await phase3_checks.noncoded_drug_not_in_odoo(x)
    assert status == "PASS", detail
    assert "sale_order_lines=0" in detail


# ─── cleanup ─────────────────────────────────────────────────────────────────


async def test_cleanup_aborts_when_the_tunnel_is_down(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sm_common.integrations.exceptions import TransientError

    x = _ctx(FakeLab())
    ran: list[str] = []

    def down(label: str) -> Any:
        async def fn() -> str | None:
            ran.append(label)
            raise TransientError("bahmni: transport error")

        return fn

    for i in range(4):
        x.later(0, "patient", f"{phase3_checks.NAME} P{i}", down(f"P{i}"))
    done, total = await phase3_checks._cleanup(x)
    out = capsys.readouterr().out
    assert (done, total) == (0, 4)
    assert len(ran) == 3, "cleanup kept calling a dead tunnel"
    assert "CLEANUP ABORTED tunnel down: 1 left" in out
    assert f"find them by the name {phase3_checks.NAME} " in out
    assert "LEFT patient" in out and "void the patient" in out


async def test_adt_cleanup_incomplete_names_the_run_tag_even_when_no_check_ran(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # HARNESS_ONLY matching nothing: no check runs, so no loop variable is ever bound.
    # The CLEANUP INCOMPLETE line must still name the synthetic-record tag, and the
    # SUMMARY and exit code must still come out.
    adt_checks = phase3_checks.adt_checks
    monkeypatch.setenv("HARNESS_ONLY", "no_such_check")
    monkeypatch.delenv("HARNESS_SAMPLES_DIR", raising=False)

    async def partial(_x: Any, name: str = adt_checks.NAME) -> tuple[int, int]:
        return 0, 1

    monkeypatch.setattr(adt_checks, "_cleanup", partial)
    cfg = {"harness": {"provider_uuid": "", "tag": "LT-ADAPTER"}, "field_mapping": {}}
    config = BahmniConfig(
        location_uuid="loc", identifier_type_uuid="idt", identifier_source_uuid="ids"
    )
    code = await adt_checks.run(cfg, _bahmni_http(), config)
    out = capsys.readouterr().out
    assert code == 4
    assert f"CLEANUP INCOMPLETE 1 left; find them by the name {adt_checks.NAME} " in out
    assert "SUMMARY adt pass=0" in out and "of=0" in out


async def test_adt_cleanup_incomplete_names_the_run_tag_not_the_last_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adt_checks = phase3_checks.adt_checks
    only = adt_checks.ADT_CHECKS[0][0]
    monkeypatch.setenv("HARNESS_ONLY", only)
    monkeypatch.delenv("HARNESS_SAMPLES_DIR", raising=False)

    async def one(_x: Any, _name: str, _fn: Any) -> tuple[str, str]:
        return "PASS", "fake"

    async def partial(_x: Any, name: str = adt_checks.NAME) -> tuple[int, int]:
        return 0, 1

    monkeypatch.setattr(adt_checks, "_one", one)
    monkeypatch.setattr(adt_checks, "_cleanup", partial)
    cfg = {"harness": {"provider_uuid": "", "tag": "LT-ADAPTER"}, "field_mapping": {}}
    config = BahmniConfig(
        location_uuid="loc", identifier_type_uuid="idt", identifier_source_uuid="ids"
    )
    await adt_checks.run(cfg, _bahmni_http(), config)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("CLEANUP INCOMPLETE"))
    assert f"find them by the name {adt_checks.NAME} " in line
    assert f"by the name {only} " not in line


# ─── env only ────────────────────────────────────────────────────────────────


def test_harness_reads_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*ELIS_ENV, "ELIS_CA_PEM", "ELIS_INSECURE_TLS"]:
        monkeypatch.delenv(name, raising=False)
    assert phase3_checks.missing_lab_env() == list(ELIS_ENV)
    with pytest.raises(phase3_checks.MissingEnv) as missing:
        phase3_checks.elis_config_from_env()
    assert missing.value.names == list(ELIS_ENV)
    for k, v in ELIS_ENV.items():
        monkeypatch.setenv(k, v)
    assert phase3_checks.missing_lab_env() == []
    cfg = phase3_checks.elis_config_from_env()
    assert (cfg.base_url, cfg.username, cfg.password) == (
        ELIS_ENV["ELIS_BASE_URL"], ELIS_ENV["ELIS_USER"], ELIS_ENV["ELIS_PASS"]
    )
    assert cfg.guard_header_value == ELIS_ENV["ELIS_GUARD_KEY"]
    assert cfg.ro_dsn == ELIS_ENV["ELIS_RO_DSN"]
    assert cfg.accession_prefix == ELIS_ENV["ELIS_ACCESSION_PREFIX"]
    assert cfg.verify_tls is True
    # Odoo is optional and all-or-nothing.
    for k in phase3_checks.ODOO_ENV:
        monkeypatch.delenv(k, raising=False)
    assert phase3_checks.odoo_env() is None

    src = (HARNESS / "phase3_checks.py").read_text()
    # No default for any lab/Odoo setting, no hosts, no uuids, synthetic names only.
    assert not re.search(r"\b(?:get|getenv|setdefault)\(\s*\"(ELIS|ODOO)_[A-Z_]+\"\s*,", src)
    assert not re.search(r"https?://[A-Za-z0-9]", src)
    assert not UUID_LITERAL.findall(src)
    assert "LT-P3-" in src
    # The library reads no env: config arrives from the caller.
    lib = ROOT / "sm_common" / "integrations"
    for f in sorted(lib.rglob("*.py")):
        text = f.read_text()
        assert "os.environ" not in text and "getenv" not in text, f"{f.name} reads env"


# ─── encounter time ──────────────────────────────────────────────────────────


class FakeRecord:
    """create_patient / start_visit / write_encounter, with the server clock ahead."""

    def __init__(self, server_ahead_s: float) -> None:
        self.start = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=server_ahead_s)
        self.writes: list[RecordEncounterInput] = []

    async def create_patient(self, _p: Any) -> tuple[Any, bool]:
        return SimpleNamespace(uuid="p-1", identifier="ID-1"), True

    async def start_visit(self, _patient: str) -> tuple[RecordVisit, bool]:
        return RecordVisit("v-1", "OPD", self.start, None, "loc"), True

    async def write_encounter(self, e: RecordEncounterInput) -> RecordEncounterResult:
        self.writes.append(e)
        return RecordEncounterResult(encounter_uuid=str(e.note_id), created=True)


@pytest.mark.parametrize("ahead_s", [5.0, 0.0, -5.0])
async def test_encounter_written_at_or_after_the_visit_start(ahead_s: float) -> None:
    """A server clock ahead of ours would make 'now' precede the visit start, which the
    client refuses as EncounterOutsideVisit (terminal) before any write."""
    x = _ctx(FakeLab())
    fake = FakeRecord(ahead_s)
    x.record = fake  # type: ignore[assignment]
    patient, visit = await x.p3_patient("Orders")
    await x.encounter(patient, visit)
    (written,) = fake.writes
    assert written.encounter_at >= fake.start
    assert written.encounter_at <= max(fake.start, datetime.now(UTC))
