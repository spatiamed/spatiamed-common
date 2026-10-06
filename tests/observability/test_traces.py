"""Plan 4 trace vectors T1-T8 through REAL sentry_sdk integrations (FastAPI/Starlette,
SQLAlchemy, httpx, redis). Each vector must also FAIL with its control disabled."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import redis
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sqlalchemy import create_engine, text

from sm_common.observability import _controls, init_error_tracking, traces
from tests.observability.conftest import DUMMY_DSN, fake_email, fake_name, fake_phone, nonce


class TxTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.transactions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            payload = item.payload.json
            if payload is None:
                continue
            payload = json.loads(json.dumps(payload, default=str))
            if item.headers.get("type") == "transaction":
                self.transactions.append(payload)
            elif item.headers.get("type") == "event":
                self.events.append(payload)

    def blob(self) -> str:
        return json.dumps(self.transactions)

    def spans(self, op_prefix: str) -> list[dict[str, Any]]:
        return [
            s
            for t in self.transactions
            for s in t.get("spans", [])
            if str(s.get("op", "")).startswith(op_prefix)
        ]


TraceCapture = Callable[..., Any]


class _FakeRedisConn(redis.connection.Connection):
    """No server: the sentry redis integration wraps Redis.execute_command, above this."""

    def connect(self) -> None: ...
    def send_command(self, *a: Any, **k: Any) -> None: ...
    def send_packed_command(self, *a: Any, **k: Any) -> None: ...
    def read_response(self, *a: Any, **k: Any) -> bytes:
        return b"v"

    def disconnect(self, *a: Any, **k: Any) -> None: ...
    def can_read(self, *a: Any, **k: Any) -> bool:
        return False


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> Iterator[TraceCapture]:
    # Deterministic sampling: our own coin always lands "sampled".
    monkeypatch.setattr(traces.random, "random", lambda: 0.0)

    @contextmanager
    def _capture(
        disable: tuple[str, ...] = (), rate: object = "0.1", targets: str | None = None
    ) -> Iterator[TxTransport]:
        transport = TxTransport()
        with _controls.disabled(*disable):
            init_error_tracking(
                dsn=DUMMY_DSN,
                environment="prod",
                release="sha-test",
                service_name="unit",
                transport=transport,
                traces_sample_rate=rate,
                trace_propagation_targets=targets,
            )
            yield transport
            sentry_sdk.flush()

    yield _capture
    sentry_sdk.get_global_scope().set_client(None)


def _app(engine: Any, outbound: httpx.Client, r: redis.Redis) -> FastAPI:
    app = FastAPI()

    @app.get("/t/{capability}/status")
    def status(capability: str) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/patients/by-name/{name}")
    def by_name(name: str) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/work")
    def work(phone: str, name: str, email: str) -> dict[str, bool]:
        with engine.connect() as c:
            c.execute(text("select :p as phone"), {"p": phone})
            c.execute(text(f"select '{name}' as n, '{email}' as e, 98765 as num"))
        outbound.get(f"https://api.exotel.com/v1/Calls/{phone}", params={"To": phone, "Body": name})
        r.get(f"otp:{phone}:{name.split()[0]}")
        return {"ok": True}

    @app.get("/chart")
    def chart(mrn: str) -> dict[str, bool]:
        outbound.get(f"https://hms.example.org/api/patients/{mrn}/notes")
        return {"ok": True}

    return app


@pytest.fixture
def world() -> Iterator[tuple[FastAPI, list[dict[str, str]]]]:
    seen: list[dict[str, str]] = []
    outbound = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: (seen.append(dict(req.headers)), httpx.Response(200))[1]
        )
    )
    r = redis.Redis(connection_pool=redis.ConnectionPool(connection_class=_FakeRedisConn))
    yield _app(create_engine("sqlite://"), outbound, r), seen


def _work(app: FastAPI) -> tuple[str, str, str]:
    phone, name, email = fake_phone(), fake_name(), fake_email()
    TestClient(app).get("/work", params={"phone": phone, "name": name, "email": email})
    return phone, name, email


# T1: unmatched route -> SDK names the transaction with the raw URL (source "url").
# A name in a path segment is invisible to every regex; only the constant name saves it.
def case_t1_unmatched_route_name(
    traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()
) -> None:
    app, _ = world
    cap, who = f"cap{nonce()}{nonce()}", f"Zebulon{nonce()}"
    with traced(disable) as t:
        TestClient(app).get(f"/t/{cap}/nope")
        TestClient(app).get(f"/lookup/{who}")
    assert len(t.transactions) == 2, "no transaction captured"
    assert cap not in t.blob() and who not in t.blob()


# T2: SQL literals in db span descriptions (and the query breadcrumb)
def case_t2_sql_literals(traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()) -> None:
    app, _ = world
    with traced(disable) as t:
        phone, name, email = _work(app)
    db = t.spans("db")
    assert any("select" in str(s.get("description", "")).lower() for s in db)
    blob = json.dumps(db)
    # phone = a BOUND parameter (SDK drops params); the rest are literals (we strip them)
    for s in (phone, name.split()[0], email, "98765"):
        assert s not in blob


# T3: outbound http span: path phone, query (data http.query), url data, and a record
# id in the path (an MRN: no regex in the walk knows it, only scrub_url's {id} does)
def case_t3_http_span(traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()) -> None:
    app, _ = world
    mrn = "4" + "".join(random.choices("0123456789", k=6))
    with traced(disable) as t:
        phone, name, _ = _work(app)
        TestClient(app).get("/chart", params={"mrn": mrn})
    http = t.spans("http.client")
    assert http and "api.exotel.com" in json.dumps(http)
    assert "hms.example.org/api/patients/{id}/notes" in json.dumps(http)
    blob = json.dumps(http)
    for s in (phone, name.split()[0], "To=", "?", mrn):
        assert s not in blob


# T4: redis key (description, tags.redis.key)
def case_t4_redis_key(traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()) -> None:
    app, _ = world
    with traced(disable) as t:
        phone, name, _ = _work(app)
    spans = t.spans("db.redis")
    assert spans and spans[0]["description"].startswith("GET")
    assert phone not in json.dumps(spans) and name.split()[0] not in json.dumps(spans)


# T5: transaction request: query_string + raw url (path params) on a MATCHED route
def case_t5_request(traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()) -> None:
    app, _ = world
    cap, who = f"cap{nonce()}{nonce()}", f"Zebulon{nonce()}"
    with traced(disable) as t:
        TestClient(app).get(f"/t/{cap}/status?name={who}")
        TestClient(app).get(f"/patients/by-name/{who}")
    names = sorted(tx["transaction"] for tx in t.transactions)
    assert names == ["/patients/by-name/{name}", "/t/{capability}/status"]
    assert cap not in t.blob() and who not in t.blob()


# T6: sentry-trace/baggage never reach a vendor (SDK default target is ".*")
def case_t6_no_vendor_propagation(
    traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()
) -> None:
    app, seen = world
    with traced(disable, targets="qc.staging.spatiamed.com"):
        _work(app)
    assert seen, "outbound call not made"
    for headers in seen:
        assert "sentry-trace" not in headers and "baggage" not in headers


# T7: custom transaction / span names built from data
def case_t7_custom_names(traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()) -> None:
    phone, email = fake_phone(), fake_email()
    with (
        traced(disable) as t,
        sentry_sdk.start_transaction(op="task", name=f"reminder {phone}"),
        sentry_sdk.start_span(op="function", name=f"lookup {email}"),
    ):
        pass
    assert t.transactions
    assert phone not in t.blob() and email not in t.blob()


# T8: the same SQL literal as a "query" BREADCRUMB on an ERROR event (tracing off).
# Plan 2's walk only regex-redacts it, so a name in a literal survived until v0.18.0.
def case_t8_sql_breadcrumb_on_error(
    traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()
) -> None:
    who = f"Zebulon{nonce()}"
    engine = create_engine("sqlite://")
    with traced(disable, rate=None) as t:
        with engine.connect() as c:
            c.execute(text(f"select '{who}' as n"))
        sentry_sdk.capture_exception(RuntimeError(f"GTT8{nonce()}"))
    blob = json.dumps(t.events)
    assert '"category": "query"' in blob
    assert who not in blob


# T9: stdlib subprocess span: the SDK puts the whole argv in the description.
def case_t9_subprocess_args(
    traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()
) -> None:
    who = f"Zebulon{nonce()}"
    with traced(disable) as t, sentry_sdk.start_transaction(op="task", name="export"):
        subprocess.run([sys.executable, "-c", "pass", who], check=True)  # noqa: S603
    spans = t.spans("subprocess")
    assert spans, "no subprocess span captured"
    assert who not in t.blob()


# T10: app code attaching data/tags to a span (span.set_data / set_tag). A bare name
# is invisible to every regex; only the data/tag allowlist removes it.
def case_t10_span_data_allowlist(
    traced: TraceCapture, world: Any, disable: tuple[str, ...] = ()
) -> None:
    who = f"Zebulon{nonce()}"
    with (
        traced(disable) as t,
        sentry_sdk.start_transaction(op="task", name="reminder"),
        sentry_sdk.start_span(op="function", name="lookup") as span,
    ):
        span.set_data("patient.name", who)
        span.set_tag("patient", who)
    assert t.spans("function"), "no span captured"
    assert who not in t.blob()


VECTORS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "T1": (case_t1_unmatched_route_name, ("transaction_name",)),
    "T2": (case_t2_sql_literals, ("span_sql",)),
    "T3": (case_t3_http_span, ("span_http",)),
    "T4": (case_t4_redis_key, ("span_redis", "span_data_allowlist")),
    "T5": (case_t5_request, ("structural",)),
    "T6": (case_t6_no_vendor_propagation, ("no_trace_propagation",)),
    "T7": (case_t7_custom_names, ("span_text", "walk")),
    "T8": (case_t8_sql_breadcrumb_on_error, ("span_sql",)),
    "T9": (case_t9_subprocess_args, ("span_subprocess",)),
    "T10": (case_t10_span_data_allowlist, ("span_data_allowlist",)),
}


@pytest.mark.parametrize("vector", sorted(VECTORS))
def test_trace_vector_is_scrubbed(vector: str, traced: TraceCapture, world: Any) -> None:
    VECTORS[vector][0](traced, world)


@pytest.mark.parametrize("vector", sorted(VECTORS))
def test_trace_vector_fails_without_its_defence(
    vector: str, traced: TraceCapture, world: Any
) -> None:
    fn, controls = VECTORS[vector]
    with pytest.raises(AssertionError):
        fn(traced, world, disable=controls)


def test_own_host_gets_propagation_headers(traced: TraceCapture) -> None:
    seen: list[dict[str, str]] = []
    c = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (seen.append(dict(r.headers)), httpx.Response(200))[1]
        )
    )
    with (
        traced(targets="qc.staging.spatiamed.com"),
        sentry_sdk.start_transaction(op="task", name="t"),
    ):
        c.get("https://qc.staging.spatiamed.com/api/x")
        c.get("https://qc.staging.spatiamed.com.evil.io/api/x")
        c.get("https://evil.io/?u=https://qc.staging.spatiamed.com/")
    assert "sentry-trace" in seen[0]
    assert "sentry-trace" not in seen[1] and "sentry-trace" not in seen[2]


def test_default_rate_is_off_and_errors_still_flow(traced: TraceCapture, world: Any) -> None:
    app, _ = world
    with traced(rate=None) as t:
        _work(app)
        sentry_sdk.capture_exception(RuntimeError(f"GTT{nonce()}"))
    assert t.transactions == [] and len(t.events) == 1
    assert sentry_sdk.get_client().options["traces_sampler"] is None


def test_scrubber_crash_drops_transaction(
    traced: TraceCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(traces, "_scrub_span", lambda s: (_ for _ in ()).throw(RuntimeError("bug")))
    with (
        traced() as t,
        sentry_sdk.start_transaction(op="task", name="x"),
        sentry_sdk.start_span(op="function", name="y"),
    ):
        pass
    assert t.transactions == []


def test_ai_integrations_disabled(traced: TraceCapture) -> None:
    import importlib

    from sm_common.observability.init import _DISABLED_INTEGRATIONS

    with traced():
        disabled = set(sentry_sdk.get_client().options["disabled_integrations"])
    for path in _DISABLED_INTEGRATIONS:
        module, _, cls = path.rpartition(".")
        try:
            klass = getattr(importlib.import_module(module), cls)
        except Exception:  # noqa: BLE001 - library not installed here
            continue
        assert klass in disabled, path
