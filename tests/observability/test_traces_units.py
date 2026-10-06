import re

import pytest

from sm_common.observability.traces import (
    TRACES_SAMPLE_RATE_CEILING,
    UNMATCHED_TRANSACTION,
    make_traces_sampler,
    parse_propagation_targets,
    parse_sample_rate,
    scrub_url,
    strip_sql_literals,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0.0),
        ("", 0.0),
        ("0", 0.0),
        ("0.1", 0.1),
        (0.25, 0.25),
        ("1.0", TRACES_SAMPLE_RATE_CEILING),
        ("5", TRACES_SAMPLE_RATE_CEILING),
        ("-1", 0.0),
        ("nan", 0.0),
        ("abc", 0.0),
        ("inf", TRACES_SAMPLE_RATE_CEILING),
    ],
)
def test_parse_sample_rate(raw: object, expected: float) -> None:
    assert parse_sample_rate(raw) == expected


def test_targets_default_none_and_anchored() -> None:
    assert parse_propagation_targets(None) == []
    assert parse_propagation_targets("") == []
    [rx] = parse_propagation_targets("https://qc.staging.spatiamed.com/")
    assert re.search(rx, "https://qc.staging.spatiamed.com/api")
    assert not re.search(rx, "https://qc.staging.spatiamed.com.evil.io/")
    assert not re.search(rx, "https://evil.io/?next=https://qc.staging.spatiamed.com/")
    assert len(parse_propagation_targets("http://queuecare-server:8000")) == 1
    for bad in ("*", ".*", "https://x.com/path", "^evil", "a|b"):
        assert parse_propagation_targets(bad) == []
    assert len(parse_propagation_targets("*, api.staging.spatiamed.com")) == 1


def test_sampler_ignores_forged_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    from sm_common.observability import traces

    monkeypatch.setattr(traces.random, "random", lambda: 0.99)
    s = make_traces_sampler(0.1)
    assert s({"parent_sampled": True, "asgi_scope": {"type": "http", "path": "/x"}}) is False
    assert s({"parent_sampled": True, "celery_job": {}}) is True
    monkeypatch.setattr(traces.random, "random", lambda: 0.0)
    assert s({"asgi_scope": {"type": "websocket", "path": "/ws"}}) is False
    for probe in ("/health", "/health/ready", "/health/version", "/api/kiosk/health"):
        assert s({"asgi_scope": {"type": "http", "path": probe}}) is False
    assert s({"asgi_scope": {"type": "http", "path": "/api/healthcare-plans"}}) is True
    assert s({"asgi_scope": {"type": "http", "path": "/api/x"}}) is True
    assert make_traces_sampler(5)({}) is True  # clamped, still a valid rate
    assert make_traces_sampler(0)({}) is False


@pytest.mark.parametrize(
    ("sql", "kept", "gone"),
    [
        ("select 'Zeb O''Brien' as n where id = 42", "select ? as n where id = ?", ["Zeb", "42"]),
        ("select * from t where a = $1 and b = %(b)s and c = :c", "$1", []),
        ("select E'x\\'y', $$secret body$$, $tag$more$tag$", "select", ["secret", "more"]),
        (
            "select 1 /* phone 9876543210 */ -- email a@b.co\n from t",
            "from t",
            ["9876543210", "a@b.co"],
        ),
        ("insert into t values (1, 2, 3) where x in (1,2,3)", "IN (?)", []),
        ("select col_2, t1.x from t1", "col_2, t1.x", []),
    ],
)
def test_strip_sql_literals(sql: str, kept: str, gone: list[str]) -> None:
    out = strip_sql_literals(sql)
    assert kept in out
    for g in gone:
        assert g not in out


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://u:pw@api.exotel.com/v1/Calls/9876543210?To=1#f",
            "https://api.exotel.com/v1/Calls/[PHONE]",
        ),
        ("https://qc.x.com/t/capabc123/status", "https://qc.x.com/t/[REDACTED]/status"),
        ("https://x.com/api/patients/123456/notes", "https://x.com/api/patients/{id}/notes"),
        (
            "https://x.com/v2/users/b1e2c3d4-0000-4000-8000-000000000001",
            "https://x.com/v2/users/b1e2c3d4-0000-4000-8000-000000000001",
        ),
    ],
)
def test_scrub_url(url: str, expected: str) -> None:
    assert scrub_url(url) == expected


def test_unmatched_constant() -> None:
    assert UNMATCHED_TRANSACTION == "<unmatched route>"
