"""The --adt harness stops for good when a signal lands during a box-wide restore.

A cancellation caught inside ``_restore`` (it retries through anything) must still stop
the run: no further check, above all no further box-wide probe, may start.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import signal
import sys
from typing import Any

import httpx
import pytest

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"))
import adt_checks  # noqa: E402

GP = "emrapi.visitExpireHours"
GP_ID = "gp-1"


def _bahmni(state: dict[str, Any]) -> httpx.MockTransport:
    async def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/session"):
            return httpx.Response(
                200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=x"}
            )
        if req.method == "POST" and req.url.path.endswith(f"/systemsetting/{GP_ID}"):
            value = __import__("json").loads(req.content)["value"]
            if value == state["original"] and not state["signalled"]:
                # The operator presses Ctrl-C while the first restore is in flight.
                state["signalled"] = True
                os.kill(os.getpid(), signal.SIGINT)
                await asyncio.sleep(0.05)
            state["value"] = value
            return httpx.Response(200, json={})
        if req.url.path.endswith("/systemsetting"):
            return httpx.Response(
                200,
                json={"results": [{"uuid": GP_ID, "property": GP, "value": state["value"]}]},
            )
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
async def test_signal_during_restore_stops_every_later_probe(tmp_path, monkeypatch):
    state: dict[str, Any] = {"original": "12", "value": "12", "signalled": False}
    calls: list[str] = []

    async def box_wide_probe(x: adt_checks.AdtCtx) -> adt_checks.Result:
        calls.append("probe")
        entry = adt_checks._remember(
            x, {"kind": "gp", "what": "GP set", "uuid": GP_ID, "property": GP, "original": "12"}
        )
        try:
            await x.raw("POST", f"systemsetting/{GP_ID}", body={"value": "0"}, admin=True)
        finally:
            await adt_checks._restore(x, entry)
        return "PASS", "probe"

    async def stale_close(x: adt_checks.AdtCtx) -> adt_checks.Result:
        calls.append("stale_close")
        return "PASS", "must never run"

    monkeypatch.setenv("HARNESS_SAMPLES_DIR", str(tmp_path))
    monkeypatch.setenv(adt_checks.GLOBAL_ENV, "1")
    monkeypatch.delenv("HARNESS_ONLY", raising=False)
    monkeypatch.setattr(
        adt_checks,
        "ADT_CHECKS",
        [("probe", "test_topic", box_wide_probe), ("stale", "test_topic", stale_close)],
    )
    http = BahmniHttp("https://b.test", "u", "p", transport=_bahmni(state))
    admin = BahmniHttp("https://b.test", "a", "p", transport=_bahmni(state))
    config = BahmniConfig(location_uuid="L", identifier_type_uuid="t", identifier_source_uuid="s")
    loop = asyncio.get_running_loop()
    try:
        code = await adt_checks.run({"harness": {"provider_uuid": "prov"}}, http, config, admin)
    finally:
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    assert state["signalled"] and state["value"] == "12", "the restore still completed"
    assert calls == ["probe"], "no check may start after the signal"
    assert code == 130


def test_needs_global_refuses_once_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SAMPLES_DIR", str(tmp_path))
    monkeypatch.setenv(adt_checks.GLOBAL_ENV, "1")
    http = BahmniHttp("https://b.test", "u", "p")
    config = BahmniConfig(location_uuid="L", identifier_type_uuid="t", identifier_source_uuid="s")
    x = adt_checks.AdtCtx({"harness": {"provider_uuid": "prov"}}, http, config, http)
    assert adt_checks._needs_global(x) is None
    x.cancelled = True
    assert "interrupted" in (adt_checks._needs_global(x) or "")
