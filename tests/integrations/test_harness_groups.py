"""verify_adapter_live.py runs the lab result, radiology and inpatient care groups."""

from __future__ import annotations

import importlib
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"
sys.path.insert(0, str(HARNESS))
import adt_checks  # noqa: E402
import verify_adapter_live as live  # noqa: E402


@pytest.mark.parametrize(
    ("flag", "module"),
    [
        ("--lab-results", "lab_result_checks"),
        ("--radiology", "radiology_checks"),
        ("--ipd-care", "ipd_care_checks"),
    ],
)
def test_group_flags_select_their_module(flag: str, module: str) -> None:
    assert live.group_module([flag]) == module
    mod = importlib.import_module(module)
    assert callable(getattr(mod, "run_checks", None))


def test_no_group_flag_selects_nothing() -> None:
    assert live.group_module([]) is None
    assert live.group_module(["--adt"]) is None


def test_two_group_flags_are_refused() -> None:
    with pytest.raises(SystemExit):
        live.group_module(["--radiology", "--ipd-care"])


def _fake(rows: list[Any], *, state: dict[str, Any] | None = None) -> tuple[Any, Any]:
    ctx = SimpleNamespace(state=state or {}, cleanups=[], calls=3, writes=1)

    async def run_checks(c: Any) -> list[Any]:
        assert c is ctx
        return rows

    return SimpleNamespace(run_checks=run_checks), ctx


def _cleanup_spy(monkeypatch: pytest.MonkeyPatch, result: tuple[int, int]) -> list[Any]:
    calls: list[Any] = []

    async def spy(x: Any, name: str = "") -> tuple[int, int]:
        calls.append(x)
        return result

    monkeypatch.setattr(adt_checks, "_cleanup", spy)
    return calls


async def test_group_prints_rows_in_order_and_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _cleanup_spy(monkeypatch, (0, 0))
    rows = [("b_check", "topic_b", ("PASS", "ok")), ("a_check", "topic_a", ("INFO", "n=2"))]
    module, ctx = _fake(rows, state={"rad_cleanup": (2, 2)})
    assert await live.run_group("radiology_checks", module, ctx) == 0
    out = capsys.readouterr().out.splitlines()
    checks = [line for line in out if line.startswith("CHECK ")]
    assert checks == ["CHECK b_check PASS topic_b ok", "CHECK a_check INFO topic_a n=2"]
    assert any(line.startswith("SUMMARY radiology_checks pass=1") for line in out)
    # Radiology cleans up inside run_checks; the runner must not clean up again.
    assert calls == []


async def test_lab_results_group_cleanup_runs_in_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _cleanup_spy(monkeypatch, (3, 3))
    module, ctx = _fake([("gate", "t", ("PASS", ""))])
    assert await live.run_group("lab_result_checks", module, ctx) == 0
    assert calls == [ctx]


async def test_lab_results_cleanup_runs_even_when_checks_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _cleanup_spy(monkeypatch, (1, 1))
    ctx = SimpleNamespace(state={}, cleanups=[], calls=0, writes=0)

    async def boom(c: Any) -> list[Any]:
        raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError):
        await live.run_group("lab_result_checks", SimpleNamespace(run_checks=boom), ctx)
    assert calls == [ctx]


@pytest.mark.parametrize("status", ["FAIL", "INCONCLUSIVE"])
async def test_fail_or_inconclusive_exits_1(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _cleanup_spy(monkeypatch, (0, 0))
    module, ctx = _fake([("x", "t", ("PASS", "")), ("y", "t", (status, "why"))])
    assert await live.run_group("ipd_care_checks", module, ctx) == 1


@pytest.mark.parametrize(
    ("group", "state", "cleanup"),
    [
        ("radiology_checks", {"rad_cleanup": (1, 2)}, (0, 0)),
        ("ipd_care_checks", {"ipd_cleanup": (0, 5)}, (0, 0)),
        ("lab_result_checks", {}, (2, 3)),
    ],
)
async def test_incomplete_cleanup_exits_4(
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    state: dict[str, Any],
    cleanup: tuple[int, int],
) -> None:
    _cleanup_spy(monkeypatch, cleanup)
    module, ctx = _fake([("x", "t", ("PASS", ""))], state=state)
    assert await live.run_group(group, module, ctx) == 4
