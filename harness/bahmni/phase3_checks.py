"""The ``--orders`` and ``--lab`` check groups of ``verify_adapter_live.py``.

``--orders`` drives the orders surface of ``BahmniRecordClient``: a
visit's drug and lab orders, the fulfiller-status mirror, the lab test catalogue, and
(only when every ``ODOO_*`` variable is set) whether the billing system turned our
non-coded drug order into a sale line. ``--lab`` drives ``OpenElisClient`` against
the lab system: login, lab order to sample, the proxy guard, a verified collect, the
OpenMRS feed back, a duplicate accession and a test added between read and send.
One line per check:

    CHECK <name> PASS|FAIL|INCONCLUSIVE|INFO <topic> <detail>

- ``PASS``: as the plan expects; a record-only check prints its value after ``record``.
- ``FAIL``: the expectation does not hold; the client's fallback for that topic applies.
- ``INCONCLUSIVE``: the probe could not decide (an earlier check did not complete, or a
  precondition on the box is missing).
- ``INFO``: an optional check that was not configured (no ``ODOO_*`` env).
Cleanup is the ADT runner's: after 3 transport errors in a row it stops and prints a
``LEFT`` line, with the manual step, for each record to clean by hand.
Exit: 0 when nothing FAILed or was INCONCLUSIVE; 1 otherwise or on an early stop;
2 bad env (missing variables, or the ``lab`` extra is not installed); 4 cleanup
incomplete; 130 interrupted.

Config is env only (no defaults): the Bahmni variables as for the other groups, plus
for ``--lab`` ``ELIS_BASE_URL``, ``ELIS_USER``, ``ELIS_PASS``, ``ELIS_GUARD_KEY``,
``ELIS_RO_DSN`` and ``ELIS_ACCESSION_PREFIX`` (optional ``ELIS_CA_PEM`` path or
``ELIS_INSECURE_TLS=1``), and for the optional Odoo read ``ODOO_URL``, ``ODOO_DB``,
``ODOO_USER`` and ``ODOO_PASS``.

Safety:
- Every patient it creates is synthetic and named ``LT-P3-<RUN>``; they are voided at
  the end. Lab-system samples cannot be removed (the lab DB role is read only); they stay
  as uncollected or collected synthetic samples of voided patients.
- The collect call is destructive. The harness never builds it: every collect goes
  through ``OpenElisClient.collect``, which sends the full test list it read just
  before. ``added_test_not_cancelled`` adds a test between that read and the send on
  purpose (its own patient), and expects the client to raise ``lab_tests_changed``.
- The guard probe sends requests WITHOUT the guard header; it only ever names the read
  provider (``sampleId=0``), so a broken guard exposes nothing and changes nothing.
- Detail text is meant for a public PR: counts, booleans, status codes, fixed error
  codes, schema column names. Server messages and bodies go to ``HARNESS_SAMPLES_DIR``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import re
import ssl
import time
import xmlrpc.client
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import adt_checks
import httpx
from adt_checks import AdtCtx, inconclusive, passed, record, scrub

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.openelis import (
    PROVIDER_SAMPLE_TESTS,
    CollectResult,
    OpenElisClient,
    OpenElisConfig,
    PendingSample,
    SampleTests,
    parse_pg_duration,
    validate_ro_dsn,
)
from sm_common.integrations.exceptions import (
    AuthError,
    ConflictError,
    HmsAdapterError,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import (
    RecordAddress,
    RecordDrugOrder,
    RecordEncounterInput,
    RecordLabOrder,
    RecordLabTest,
    RecordPatientInput,
)

TAG = "LT-P3-"
# Letters only: OpenMRS person names may refuse digits, and no uuid-like run appears.
RUN = "".join(chr(ord("A") + int(ch, 16)) for ch in uuid4().hex[:6])
NAME = f"{TAG}{RUN}"
DRUG_NAME = f"{NAME} Synthetic Drug"
# Our accession numbers are <prefix><yymmdd><nnnn>; the harness uses 9xxx so a run
# never takes a number the tenant counter (from 0001) would issue on the same day.
ACCESSION_BASE = 9000 + (uuid4().int % 900)
ACCESSION_MAX = 13  # the lab system's accession column / validator limit
ELIS_ENV = (
    "ELIS_BASE_URL",
    "ELIS_USER",
    "ELIS_PASS",
    "ELIS_GUARD_KEY",
    "ELIS_RO_DSN",
    "ELIS_ACCESSION_PREFIX",
)
ODOO_ENV = ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_PASS")
AJAX = "/openelis/ajaxQueryXML"
SAMPLE_WAIT_S = 30.0  # lab_order_sample: samples within 30 s
FEED_WAIT_S = 30.0  # sample_collect, collect_keeps_orders: OpenMRS hears within 30 s
ODOO_WAIT_S = 60.0  # noncoded_drug_no_sale_line: no sale line after 60 s
POLL_S = 3.0
CODE = re.compile(r"^(lab_[a-z0-9_]+|accession_conflict)$")
EVENT_SUFFIX = "/ws/rest/accession/"

Result = tuple[str, str]
OrdersLabCheck = Callable[["OrdersLabCtx"], Awaitable[Result]]
ElisFactory = Callable[[type[OpenElisClient]], OpenElisClient]


# ─── env ─────────────────────────────────────────────────────────────────────


class MissingEnv(Exception):  # noqa: N818 - it names the variables to set
    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def missing_lab_env() -> list[str]:
    return [n for n in ELIS_ENV if not os.environ.get(n)]


def elis_config_from_env() -> OpenElisConfig:
    """The lab-system config, from env only; ``MissingEnv`` names what is unset."""
    missing = missing_lab_env()
    if missing:
        raise MissingEnv(missing)
    env = os.environ
    pem_path = env.get("ELIS_CA_PEM")
    insecure = env.get("ELIS_INSECURE_TLS") == "1" and not pem_path
    return OpenElisConfig(
        base_url=env["ELIS_BASE_URL"],
        username=env["ELIS_USER"],
        password=env["ELIS_PASS"],
        guard_header_value=env["ELIS_GUARD_KEY"],
        ro_dsn=env["ELIS_RO_DSN"],
        accession_prefix=env["ELIS_ACCESSION_PREFIX"],
        verify_tls=not insecure,
        ca_bundle_pem=pathlib.Path(pem_path).read_text() if pem_path else None,
        allow_insecure_tls=insecure,
    )


def odoo_env() -> dict[str, str] | None:
    """All ``ODOO_*`` values, or None when any is unset (the Odoo read is optional)."""
    values = {n: os.environ.get(n) or "" for n in ODOO_ENV}
    return values if all(values.values()) else None


def _verify(cfg: OpenElisConfig) -> ssl.SSLContext | bool:
    if cfg.ca_bundle_pem:
        return ssl.create_default_context(cadata=cfg.ca_bundle_pem)
    return cfg.verify_tls


def code_of(exc: BaseException) -> str:
    """A fixed error code from the client (``lab_*``), else the exception class only."""
    text = str(exc)
    return text if CODE.match(text) else adt_checks.err(exc)


# ─── harness-only read-only SQL ──────────────────────────────────────────────

# Reads the library has no query for (record-only checks). Fixed text, parameters only.
HARNESS_SQL: Mapping[str, str] = {
    # sample_collect: which event_records column names the sample's encounter.
    "event_row": "SELECT e.* FROM clinlims.event_records e WHERE e.id = $1::bigint",
}


class HarnessSql:
    """One read-only asyncpg connection for ``HARNESS_SQL`` (never prints the DSN)."""

    def __init__(self, dsn: str) -> None:
        validate_ro_dsn(dsn)
        self._dsn = dsn
        self._conn: Any = None

    def __repr__(self) -> str:
        return "HarnessSql(<dsn hidden>)"

    async def fetch(self, name: str, *args: Any) -> list[dict[str, Any]]:
        sql = HARNESS_SQL[name]
        if self._conn is None:
            import asyncpg  # the 'lab' extra; checked by main() before the run

            conn = await asyncpg.connect(self._dsn, timeout=10)
            secs = parse_pg_duration(str(await conn.fetchval("SHOW statement_timeout")))
            if secs <= 0 or secs > 5:
                await conn.close()
                raise RuntimeError("lab DB role has no statement_timeout of at most 5 s")
            self._conn = conn
        async with self._conn.transaction(readonly=True):
            rows = await self._conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def aclose(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()


# ─── context ─────────────────────────────────────────────────────────────────


class InjectingClient(OpenElisClient):
    """``collect`` with a hook between its pre-read of the tests and its send.

    The hook runs once, after the first ``sample_tests`` read (the pre-read); the read's
    result is returned unchanged, so the client sends the now-stale list. A hook that
    reports the test did not land stops the collect before anything is sent.
    """

    inject: Callable[[], Awaitable[bool]] | None = None
    fired = False

    async def sample_tests(self, sample_id: int) -> SampleTests:
        got = await super().sample_tests(sample_id)
        if not self.fired and self.inject is not None:
            self.fired = True
            if not await self.inject():
                raise NotLanded
        return got


class NotLanded(Exception):  # noqa: N818 - a probe outcome, not an error
    pass


class OrdersLabCtx(AdtCtx):
    """The ADT context's plumbing (raw calls, tap, samples) plus the lab-system clients."""

    def __init__(
        self,
        cfg: dict[str, Any],
        http: BahmniHttp,
        config: BahmniConfig,
        admin_http: BahmniHttp | None = None,
        *,
        elis_factory: ElisFactory | None = None,
        guard_transport: httpx.AsyncBaseTransport | None = None,
        raw_sql: HarnessSql | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        odoo_proxy: Callable[[str], Any] = xmlrpc.client.ServerProxy,
    ) -> None:
        super().__init__(cfg, http, config, admin_http)
        self.odoo_proxy = odoo_proxy
        self.identifiers: dict[str, str] = {}
        self.elis_factory = elis_factory
        self.guard_transport = guard_transport
        self.raw_sql = raw_sql
        self.sleep = sleep
        self.elis_clients: list[OpenElisClient] = []
        self._elis: OpenElisClient | None = None
        self.patients = 0
        # Visit start as the server stamped it: a write before it is EncounterOutsideVisit.
        self.visit_starts: dict[str, datetime] = {}

    def make_elis(self, cls: type[OpenElisClient] = OpenElisClient) -> OpenElisClient:
        if self.elis_factory is None:
            raise KeyError("lab-system config (ELIS_* env)")
        client = self.elis_factory(cls)
        self.elis_clients.append(client)
        return client

    @property
    def elis(self) -> OpenElisClient:
        if self._elis is None:
            self._elis = self.make_elis()
        return self._elis

    def accession(self, k: int) -> str:
        today = datetime.now(UTC).date()
        return f"{self.elis.config.accession_prefix}{today:%y%m%d}{ACCESSION_BASE + k:04d}"

    async def p3_patient(self, role: str) -> tuple[str, str]:
        """A new synthetic patient with an OPD visit: (patient uuid, visit uuid)."""
        rec, _ = await self.record.create_patient(
            RecordPatientInput(
                marker=uuid4(),
                given=f"{NAME} {role}",
                family=NAME,
                gender="F",
                birth_date=date(1985, 1, 1),
                birth_date_estimated=False,
                phone=None,
                address=RecordAddress(),
            )
        )
        self.patients += 1
        self.identifiers[role] = rec.identifier
        self.later(0, "patient", f"{NAME} {role}", lambda: self._void_patient(rec.uuid))
        visit, _ = await self.record.start_visit(rec.uuid)
        self.visit_starts[visit.uuid] = visit.start
        return rec.uuid, visit.uuid

    def encounter_time(self, visit: str) -> datetime:
        """Now, but never before the visit's start (the server clock may run ahead)."""
        at = datetime.now(UTC).replace(microsecond=0)
        start = self.visit_starts.get(visit)
        return max(at, start) if start is not None else at

    async def _void_patient(self, uuid: str) -> str | None:
        visits = await self.raw(
            "GET",
            "visit",
            params={"patient": uuid, "includeInactive": "false", "v": "custom:(uuid)"},
            admin=True,
        )
        for v in visits.results():
            await self.admin_record.end_visit(str(v["uuid"]))
        got = await self.raw(
            "DELETE", f"patient/{uuid}", params={"reason": f"{TAG}harness cleanup"}, admin=True
        )
        return None if got.ok or got.status == 404 else f"void {got.status}"

    async def encounter(
        self,
        patient: str,
        visit: str,
        *,
        drugs: Sequence[RecordDrugOrder] = (),
        labs: Sequence[str] = (),
    ) -> str:
        res = await self.record.write_encounter(
            RecordEncounterInput(
                note_id=uuid4(),
                patient_uuid=patient,
                visit_uuid=visit,
                provider_uuid=self.provider,
                encounter_at=self.encounter_time(visit),
                obs=[],
                diagnoses=[],
                drug_orders=list(drugs),
                lab_orders=[RecordLabOrder(concept_uuid=c) for c in labs],
            )
        )
        return res.encounter_uuid

    async def wait_samples(self, encounter: str, want: int) -> tuple[list[PendingSample], float]:
        t0 = time.monotonic()
        rows: list[PendingSample] = []
        while True:
            rows = await self.elis.pending_samples([encounter])
            waited = time.monotonic() - t0
            if len(rows) >= want or waited >= SAMPLE_WAIT_S:
                return rows, waited
            await self.sleep(POLL_S)

    async def add_test_to_sample(self, sample: PendingSample) -> bool:
        """Order one more test of ``sample``'s type IN ITS ENCOUNTER; wait until it lands."""
        ctx = self.state["added_ctx"]
        c = self.config
        got = await self.raw(
            "POST",
            "bahmnicore/bahmniencounter",
            body={
                "patientUuid": ctx["patient"],
                "visitUuid": ctx["visit"],
                "encounterUuid": sample.encounter_uuid,
                "locationUuid": c.location_uuid,
                "encounterTypeUuid": c.consultation_encounter_type_uuid,
                "providers": [{"uuid": self.provider}],
                "observations": [],
                "orders": [
                    {
                        "concept": {"uuid": ctx["extra_concept"]},
                        "orderTypeUuid": c.lab_order_type_uuid,
                        "action": "NEW",
                        "careSetting": "OUTPATIENT",
                        "urgency": "ROUTINE",
                    }
                ],
            },
        )
        self.sample("p3_added_test_encounter", {"status": got.status, "body": got.body})
        landed_in = got.body.get("encounterUuid") if isinstance(got.body, dict) else None
        if not got.ok or landed_in != sample.encounter_uuid:
            self.state["added_why"] = f"encounter write {got.status} same_encounter=False"
            return False
        t0 = time.monotonic()
        while time.monotonic() - t0 < SAMPLE_WAIT_S:
            rows = await self.elis.pending_samples([sample.encounter_uuid])
            mine = next((r for r in rows if r.sample_id == sample.sample_id), None)
            if mine is not None and len(mine.test_names) > len(sample.test_names):
                return True
            await self.sleep(POLL_S)
        self.state["added_why"] = "the lab feed did not add the test within 30 s"
        return False


def _pick_tests(tests: Sequence[RecordLabTest]) -> tuple[list[str], str | None]:
    """(one test per sample type, up to two types; a spare test of the first type)."""
    singles = [t for t in tests if not t.is_panel]
    by_type: dict[str | None, list[RecordLabTest]] = {}
    for t in singles:
        by_type.setdefault(t.sample_type, []).append(t)
    typed = [k for k in by_type if k is not None]
    keys: list[str | None] = typed[:2] if typed else [None] if singles else []
    picks = [by_type[k][0].concept_uuid for k in keys]
    # The added-test probe orders picks[0], then the spare: both of the first type.
    first = by_type[keys[0]] if keys else []
    return picks, first[1].concept_uuid if len(first) > 1 else None


# ─── --orders ────────────────────────────────────────────────────────────────


async def _orders_fixture(x: OrdersLabCtx) -> dict[str, Any]:
    if "orders" in x.state:
        return dict(x.state["orders"])
    patient, visit = await x.p3_patient("Orders")
    labs: list[str] = []
    if x.config.lab_orderable_set_uuid and x.config.lab_order_type_uuid:
        picks, _ = _pick_tests(await x.record.list_lab_tests())
        labs = picks[:1]
    drug = RecordDrugOrder(
        name=DRUG_NAME, dose="1 tablet", frequency="twice daily", duration_days=3,
        instructions="after food",
    )
    enc = await x.encounter(patient, visit, drugs=[drug], labs=labs)
    # noncoded_drug_not_in_odoo reads the billing system by patient: a drug-only patient, so the lab
    # order above (which may rightly become a sale line) cannot touch its count.
    drug_patient, drug_visit = await x.p3_patient("Drug")
    await x.encounter(drug_patient, drug_visit, drugs=[drug])
    x.state["orders"] = {
        "patient": patient,
        "visit": visit,
        "encounter": enc,
        "labs": labs,
        "drug_identifier": x.identifiers["Drug"],
        "written_at": time.monotonic(),
    }
    return dict(x.state["orders"])


async def visit_orders_read(x: OrdersLabCtx) -> Result:
    f = await _orders_fixture(x)
    orders = await x.record.list_visit_orders(f["visit"], kinds=frozenset({"drug", "lab"}))
    drugs = [o for o in orders if o.kind == "drug" and o.display_name == DRUG_NAME]
    labs = [o for o in orders if o.kind == "lab" and o.encounter_uuid == f["encounter"]]
    raw = await x.raw(
        "GET",
        "order",
        params={
            "patient": f["patient"],
            "careSetting": "OUTPATIENT",
            "t": "drugorder",
            "v": "full",
        },
    )
    x.sample("p3_drugorder_full", raw.body)
    raw_noncoded = any(r.get("drugNonCoded") == DRUG_NAME for r in raw.results())
    if drugs:
        x.state["drug_order"] = drugs[0]
    ok = (
        len(drugs) == 1
        and drugs[0].concept_uuid is None
        and raw.ok
        and raw_noncoded
        and len(labs) == len(f["labs"])
    )
    d = drugs[0] if drugs else None
    return passed(
        ok,
        f"drug_orders={len(drugs)} concept_null={d is not None and d.concept_uuid is None} "
        f"dosing_source={d.dosing_source if d else None} "
        f"dose_recovered={d is not None and d.dose is not None} "
        f"order_full_rep={raw.status} drugNonCoded_field={raw_noncoded} "
        f"lab_orders={len(labs)}/{len(f['labs'])}",
    )


async def lab_tests_catalogue(x: OrdersLabCtx) -> Result:
    tests = await x.record.list_lab_tests()
    singles = [t for t in tests if not t.is_panel]
    typed = {t.sample_type for t in singles if t.sample_type}
    return passed(
        bool(singles),
        f"record tests={len(singles)} panels={len(tests) - len(singles)} "
        f"with_sample_type={sum(1 for t in singles if t.sample_type)} sample_types={len(typed)} "
        "(lab-system mapping is proven by lab_order_to_sample in --lab)",
    )


async def fulfiller_details_write(x: OrdersLabCtx) -> Result:
    order = x.state["drug_order"]
    try:
        await x.record.set_fulfiller_status(order.order_uuid, "COMPLETED", comment="sm:dispensed")
    except WriteNotSupported:
        return "FAIL", "fulfillerdetails 404: WriteNotSupported (fallback: mirror off)"
    got = await x.raw(
        "GET",
        f"order/{order.order_uuid}",
        params={"v": "custom:(uuid,fulfillerStatus,fulfillerComment)"},
    )
    body = got.body if isinstance(got.body, dict) else {}
    writes = x.writes
    await x.record.set_fulfiller_status(order.order_uuid, "COMPLETED", comment="sm:dispensed")
    repeat_writes = x.writes - writes
    ok = got.ok and body.get("fulfillerStatus") == "COMPLETED" and repeat_writes == 0
    return passed(
        ok,
        f"read_back={got.status} status={body.get('fulfillerStatus')} "
        f"comment_kept={body.get('fulfillerComment') == 'sm:dispensed'} "
        f"repeat_writes={repeat_writes}",
    )


async def medicationdispense_absent(x: OrdersLabCtx) -> Result:
    got = await x.raw("GET", "medicationdispense")
    return passed(got.status == 404, f"GET medicationdispense -> {got.status}")


def _odoo_sale_lines(
    env: Mapping[str, str], proxy: Callable[[str], Any], partner: str, ref: str
) -> list[dict[str, Any]]:
    """Every sale line on the sale orders of one patient (Odoo partner ref or name).

    By patient, not by line name: a line the billing feed made from our drug is named
    after the product it mapped to, never after our free-text ``drugNonCoded``.
    """
    url = env["ODOO_URL"].rstrip("/")
    common = proxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(env["ODOO_DB"], env["ODOO_USER"], env["ODOO_PASS"], {})
    if not uid:
        raise AuthError("odoo login refused")
    models = proxy(f"{url}/xmlrpc/2/object")
    rows = models.execute_kw(
        env["ODOO_DB"], uid, env["ODOO_PASS"], "sale.order.line", "search_read",
        [[
            "|",
            ["order_id.partner_id.ref", "=", ref],
            ["order_id.partner_id.name", "ilike", partner],
        ]],
        {"fields": ["name", "product_id"]},
    )
    return [dict(r) for r in rows]


async def noncoded_drug_not_in_odoo(x: OrdersLabCtx) -> Result:
    env = odoo_env()
    if env is None:
        return "INFO", "ODOO_* unset: not checked (run with the billing-system login to check)"
    f = await _orders_fixture(x)
    wait = ODOO_WAIT_S - (time.monotonic() - f["written_at"])
    if wait > 0:
        await x.sleep(wait)
    lines = await asyncio.to_thread(
        _odoo_sale_lines, env, x.odoo_proxy, f"{NAME} Drug", f["drug_identifier"]
    )
    x.sample(
        "p3_odoo_drug_patient_lines",
        [{"name": r.get("name"), "product": r.get("product_id")} for r in lines],
    )
    return passed(
        not lines,
        f"sale_order_lines={len(lines)} (drug-only patient, by partner) "
        f"after_s>={ODOO_WAIT_S:.0f}",
    )


# ─── --lab ───────────────────────────────────────────────────────────────────


async def elis_login_path(x: OrdersLabCtx) -> Result:
    try:
        await x.elis._http.ensure_session()
    except AuthError:
        return "FAIL", "login refused (form login loginName/password at ValidateLogin.do)"
    return record("form login (loginName/password) at ValidateLogin.do -> JSESSIONID")


async def lab_order_to_sample(x: OrdersLabCtx) -> Result:
    picks, spare = _pick_tests(await x.record.list_lab_tests())
    if not picks:
        return inconclusive("the orderable set lists no tests")
    patient, visit = await x.p3_patient("Lab")
    enc = await x.encounter(patient, visit, labs=picks)
    orders = await x.record.list_visit_orders(visit, kinds=frozenset({"lab"}))
    mine = [o.order_uuid for o in orders if o.encounter_uuid == enc]
    rows, waited = await x.wait_samples(enc, len(picks))
    x.state["lab"] = {"patient": patient, "visit": visit, "encounter": enc, "orders": mine}
    x.state["spare"] = spare
    if rows:
        x.state["sample"] = rows[0]
    if len(rows) > 1:
        x.state["sibling"] = rows[1]
    ok = (
        len(rows) == len(picks)
        and len(mine) == len(picks)
        and all(r.encounter_uuid == enc and r.accession_number is None for r in rows)
    )
    return passed(
        ok,
        f"lab_orders={len(mine)} samples={len(rows)} sample_types_ordered={len(picks)} "
        f"uncollected={sum(1 for r in rows if r.accession_number is None)} "
        f"waited_s={waited:.0f}",
    )


async def guard_header_required(x: OrdersLabCtx) -> Result:
    cfg = x.elis.config
    read = f"provider={PROVIDER_SAMPLE_TESTS}&sampleId=0"
    probes = [
        ("GET", read),
        ("HEAD", read),
        ("GET", f"PROVIDER={PROVIDER_SAMPLE_TESTS}&sampleId=0"),
        ("GET", f"%70rovider={PROVIDER_SAMPLE_TESTS}&sampleId=0"),
        ("GET", f"provider={PROVIDER_SAMPLE_TESTS.lower()}&sampleId=0"),
    ]
    statuses: list[str] = []
    async with httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/"),
        verify=_verify(cfg),
        transport=x.guard_transport,
        follow_redirects=False,
        timeout=30,
    ) as raw:
        for method, query in probes:
            resp = await raw.request(method, f"{AJAX}?{query}")
            statuses.append(f"{method}:{resp.status_code}")
    probed = f"no_header={','.join(statuses)}"
    if not all(s.endswith(":403") for s in statuses):
        return "FAIL", probed
    # The header-less half needs no sample (sampleId=0); the with-header read does.
    sample: PendingSample | None = x.state.get("sample")
    if sample is None:
        return inconclusive(f"{probed} with_header=not run (no sample from lab_order_to_sample)")
    tests = await x.elis.sample_tests(sample.sample_id)
    return passed(bool(tests.type_ids), f"{probed} with_header=200 types={len(tests.type_ids)}")


async def collect_verified(x: OrdersLabCtx) -> Result:
    sample: PendingSample = x.state["sample"]
    accession = x.accession(1)
    try:
        res = await x.elis.collect(sample.sample_id, accession, datetime.now(UTC).date())
    except (VendorRejected, ConflictError) as exc:
        return "FAIL", f"collect raised {code_of(exc)}"
    x.state["collect"] = res
    x.state["collect_at"] = time.monotonic()
    ok = not res.already_recorded and res.accession_number == accession
    if res.already_recorded:
        # Nothing was sent: the sample already carried this accession before the call.
        return "FAIL", "already_recorded=True sent=none (the sample was collected before)"
    return passed(
        ok,
        f"sent=204 accession_recorded={res.accession_number == accession} "
        f"collection_date_set={res.collection_date is not None} "
        f"event_after_watermark=True already_recorded=False",
    )


async def accession_len(x: OrdersLabCtx) -> Result:
    res: CollectResult = x.state["collect"]
    n = len(res.accession_number)
    return passed(
        n <= ACCESSION_MAX,
        f"record len={n} max={ACCESSION_MAX} (note the analyser barcode limit by hand)",
    )


async def event_record_columns(x: OrdersLabCtx) -> Result:
    res: CollectResult = x.state["collect"]
    sample: PendingSample = x.state["sample"]
    if x.raw_sql is None:
        return inconclusive("no lab DB connection for the harness")
    rows = await x.raw_sql.fetch("event_row", res.event_id)
    if not rows:
        return "FAIL", "the verified event row is not readable"
    row = rows[0]
    suffix = EVENT_SUFFIX + sample.encounter_uuid
    ends = sorted(k for k, v in row.items() if isinstance(v, str) and v.endswith(suffix))
    holds = sorted(
        k for k, v in row.items() if isinstance(v, str) and sample.encounter_uuid in v
    )
    return passed(
        bool(ends),
        f"record columns={','.join(sorted(row))} ends_with_accession_uri={','.join(ends) or '-'} "
        f"holds_encounter_uuid={','.join(holds) or '-'}",
    )


async def sibling_orders_marked(x: OrdersLabCtx) -> Result:
    lab = x.state["lab"]
    if "sibling" not in x.state:
        return inconclusive("the catalogue gave one sample type: no sibling sample")
    t0 = x.state["collect_at"]
    marked: list[str] = []
    while True:
        orders = await x.record.list_visit_orders(lab["visit"], kinds=frozenset({"lab"}))
        ours = [o for o in orders if o.encounter_uuid == lab["encounter"]]
        marked = [o.order_uuid for o in ours if o.accession_number == lab["encounter"]]
        if len(marked) == len(ours) or time.monotonic() - t0 >= FEED_WAIT_S:
            break
        await x.sleep(POLL_S)
    return passed(
        bool(marked),
        f"record orders={len(ours)} marked_with_encounter_uuid={len(marked)} "
        f"both_marked={len(marked) == len(ours)} waited_s={time.monotonic() - t0:.0f}",
    )


async def full_list_collect_cancels_nothing(x: OrdersLabCtx) -> Result:
    lab = x.state["lab"]
    sample: PendingSample = x.state["sample"]
    wait = FEED_WAIT_S - (time.monotonic() - x.state["collect_at"])
    if wait > 0:
        await x.sleep(wait)
    orders = await x.record.list_visit_orders(lab["visit"], kinds=frozenset({"lab"}))
    active = {o.order_uuid for o in orders}
    gone = [u for u in lab["orders"] if u not in active]
    rows = await x.elis.pending_samples([lab["encounter"]])
    now = next((r for r in rows if r.sample_id == sample.sample_id), None)
    tests_kept = now is not None and len(now.test_names) == len(sample.test_names)
    return passed(
        not gone and tests_kept,
        f"orders_active={len(lab['orders']) - len(gone)}/{len(lab['orders'])} "
        f"tests_kept={tests_kept} after_s>={FEED_WAIT_S:.0f}",
    )


async def _added_fixture(x: OrdersLabCtx) -> PendingSample | None:
    spare = x.state.get("spare")
    picks, _ = _pick_tests(await x.record.list_lab_tests())
    if not spare or not picks:
        return None
    patient, visit = await x.p3_patient("Added")
    enc = await x.encounter(patient, visit, labs=picks[:1])
    rows, _ = await x.wait_samples(enc, 1)
    if not rows:
        return None
    x.state["added_ctx"] = {"patient": patient, "visit": visit, "extra_concept": spare}
    x.state["added_sample"] = rows[0]
    return rows[0]


async def added_test_not_cancelled(x: OrdersLabCtx) -> Result:
    sample: PendingSample | None = x.state.get("added_sample") or await _added_fixture(x)
    if sample is None:
        return inconclusive("no sample type with two orderable tests, or no sample appeared")
    client = x.make_elis(InjectingClient)
    assert isinstance(client, InjectingClient)

    async def inject() -> bool:
        return await x.add_test_to_sample(sample)

    client.inject = inject
    try:
        await client.collect(sample.sample_id, x.accession(2), datetime.now(UTC).date())
    except NotLanded:
        why = x.state.get("added_why", "the added test did not land")
        return inconclusive(f"{why}; nothing was sent")
    except (VendorRejected, ConflictError) as exc:
        code = code_of(exc)
        return passed(code == "lab_tests_changed", f"collect raised {code}")
    return "FAIL", "collect succeeded: the test added after the pre-read was cancelled unseen"


async def _second_uncollected(x: OrdersLabCtx) -> PendingSample | None:
    """The sibling sample, else (a one-type catalogue) the added-test patient's sample.

    The added-test sample counts only while it is still uncollected (its probe usually
    collects it): a collected one would hit the client's own accession pre-check, not
    the lab system's duplicate handling.
    """
    sibling: PendingSample | None = x.state.get("sibling")
    if sibling is not None:
        return sibling
    added: PendingSample | None = x.state.get("added_sample")
    if added is None:
        return None
    rows = await x.elis.pending_samples([added.encounter_uuid])
    now = next((r for r in rows if r.sample_id == added.sample_id), None)
    return now if now is not None and now.accession_number is None else None


async def duplicate_accession(x: OrdersLabCtx) -> Result:
    first: CollectResult = x.state["collect"]
    sibling = await _second_uncollected(x)
    if sibling is None:
        return inconclusive("no second uncollected sample (no sibling, no added-test sample)")
    acc = first.accession_number
    try:
        await x.elis.collect(sibling.sample_id, acc, datetime.now(UTC).date())
    except (VendorRejected, ConflictError) as exc:
        return record(f"refused code={code_of(exc)} (our counter + pre-check stay the guard)")
    rows = await x.elis.pending_samples([sibling.encounter_uuid])
    dup = sum(1 for r in rows if r.accession_number == acc)
    return record(f"silent_duplicate=True samples_with_accession={dup}")


ORDERS_CHECKS: list[tuple[str, str, OrdersLabCheck]] = [
    ("visit_orders_read", "visit_order_read", visit_orders_read),
    ("lab_tests_catalogue", "lab_test_catalogue", lab_tests_catalogue),
    ("fulfiller_details_write", "fulfiller_status_mirror", fulfiller_details_write),
    ("medicationdispense_absent", "fulfiller_status_mirror", medicationdispense_absent),
    # Last: it waits until 60 s after the encounter write.
    ("noncoded_drug_not_in_odoo", "noncoded_drug_no_sale_line", noncoded_drug_not_in_odoo),
]
LAB_CHECKS: list[tuple[str, str, OrdersLabCheck]] = [
    ("elis_login_path", "lab_login_path", elis_login_path),
    ("lab_order_to_sample", "lab_order_sample", lab_order_to_sample),
    ("guard_header_required", "lab_write_guard", guard_header_required),
    ("collect_verified", "sample_collect", collect_verified),
    ("accession_len", "sample_collect", accession_len),
    ("event_record_columns", "sample_collect", event_record_columns),
    ("sibling_orders_marked", "sample_collect", sibling_orders_marked),
    (
        "full_list_collect_cancels_nothing",
        "collect_keeps_orders",
        full_list_collect_cancels_nothing,
    ),
    ("added_test_not_cancelled", "collect_keeps_orders", added_test_not_cancelled),
    # Last: it collects the sibling sample with an accession already in use.
    ("duplicate_accession", "duplicate_accession", duplicate_accession),
]
GROUPS = {"orders": ORDERS_CHECKS, "lab": LAB_CHECKS}


# ─── runner ──────────────────────────────────────────────────────────────────


async def _cleanup(x: OrdersLabCtx) -> tuple[int, int]:
    # The ADT runner's: stops after 3 transport errors in a row and prints a LEFT line
    # (with the manual step) for every record still to clean, found by this run's name.
    return await adt_checks._cleanup(x, name=NAME)


async def run(group: str, x: OrdersLabCtx) -> int:
    checks = GROUPS[group]
    only = {n for n in os.environ.get("HARNESS_ONLY", "").split(",") if n}
    checks = [(n, topic, f) for n, topic, f in checks if not only or n in only]
    counts: Counter[str] = Counter()
    t0 = time.monotonic()
    deadline = t0 + float(os.environ.get("HARNESS_DEADLINE_S", "1200"))
    print(f"{group.upper()} RUN {NAME} (synthetic records carry this name)", flush=True)
    stopped = ""
    streak = 0
    adt_checks._signals(x, asyncio.current_task(), during_cleanup=False)
    try:
        for name, topic, fn in checks:
            if x.cancelled:
                stopped = "interrupted by a signal"
                break
            if time.monotonic() > deadline:
                stopped = "deadline (HARNESS_DEADLINE_S) reached"
                break
            status, detail = await adt_checks._one(x, name, fn)  # type: ignore[arg-type]
            counts[status] += 1
            print(scrub(f"CHECK {name} {status} {topic} {detail}"), flush=True)
            streak = streak + 1 if x.transport_error else 0
            if streak >= 3:
                stopped = "3 checks in a row hit transport errors (tunnel down?)"
                break
    except asyncio.CancelledError:
        stopped = "interrupted by a signal"
    finally:
        if stopped:
            print(f"STOPPED: {stopped}; remaining checks not run", flush=True)
        adt_checks._signals(x, None, during_cleanup=True)
        done, total = await _cleanup(x)
        for client in x.elis_clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        if x.raw_sql is not None:
            with contextlib.suppress(Exception):
                await x.raw_sql.aclose()
        if group == "lab":
            print(
                f"LAB SAMPLES LEFT: this run's lab-system samples stay (read-only role); "
                f"they belong to the voided {NAME} patients",
                flush=True,
            )
        print(
            f"SUMMARY {group} pass={counts['PASS']} fail={counts['FAIL']} "
            f"inconclusive={counts['INCONCLUSIVE']} info={counts['INFO']} of={len(checks)} "
            f"cleanup={done}/{total} http_calls={x.calls} writes={x.writes} "
            f"elapsed_s={time.monotonic() - t0:.0f}",
            flush=True,
        )
    if done < total:
        return 4
    if x.cancelled or stopped:
        return 130 if x.cancelled else 1
    return 0 if counts["FAIL"] == 0 and counts["INCONCLUSIVE"] == 0 else 1


async def main(
    group: str,
    cfg: dict[str, Any],
    http: BahmniHttp,
    config: BahmniConfig,
    admin_http: BahmniHttp | None,
) -> int:
    """Entry from ``verify_adapter_live.py --orders|--lab``; the caller closes ``http``."""
    factory: ElisFactory | None = None
    raw_sql: HarnessSql | None = None
    if group == "lab":
        try:
            elis_cfg = elis_config_from_env()
        except MissingEnv as exc:
            print(f"set {exc}", flush=True)
            return 2
        try:
            import asyncpg  # noqa: F401 - the 'lab' extra
        except ImportError:
            print('install the lab extra: pip install -e ".[lab]"', flush=True)
            return 2

        def make(cls: type[OpenElisClient]) -> OpenElisClient:
            return cls(elis_cfg, verify_timeout_s=20.0)

        factory = make
        raw_sql = HarnessSql(elis_cfg.ro_dsn)
    x = OrdersLabCtx(cfg, http, config, admin_http, elis_factory=factory, raw_sql=raw_sql)
    try:
        return await run(group, x)
    except (HmsAdapterError, TransientError) as exc:
        print(f"STOPPED: {adt_checks.err(exc)}", flush=True)
        return 1
