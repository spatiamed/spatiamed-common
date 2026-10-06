"""No sentry-trace / baggage header may leave the process.

sentry-sdk 2.x propagates trace context ("tracing without performance") on EVERY
outbound request once a DSN is set, even with traces_sample_rate unset. Our outbound
calls go to vendors (Gupshup, Exotel, MSG91, Gemini, Sarvam, Razorpay...), so the
headers would hand them our trace ids and baggage (release, environment, public key).

Each case makes a REAL request to a local HTTP server after init_error_tracking and
inspects the headers that arrived. requests and aiohttp are covered because the
adopting services use them (platform-api, CareLoop, QueueCare notification_service).
"""

from __future__ import annotations

import asyncio
import threading
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import httpx
import pytest
import requests
import sentry_sdk

from sm_common.observability import _controls, init_error_tracking
from tests.observability.conftest import DUMMY_DSN, MemoryTransport

TRACE_HEADERS = ("sentry-trace", "baggage")


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/vendor/send"
    srv.shutdown()
    srv.server_close()


def _httpx_sync(url: str) -> None:
    with httpx.Client() as c:
        c.get(url).raise_for_status()


def _httpx_async(url: str) -> None:
    async def go() -> None:
        async with httpx.AsyncClient() as c:
            (await c.get(url)).raise_for_status()

    asyncio.run(go())


def _requests(url: str) -> None:
    requests.get(url, timeout=5).raise_for_status()


def _urllib(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (local test server)
        r.read()


def _aiohttp(url: str) -> None:
    async def go() -> None:
        async with aiohttp.ClientSession() as s, s.get(url) as r:
            r.raise_for_status()

    asyncio.run(go())


CLIENTS: dict[str, Callable[[str], None]] = {
    "httpx": _httpx_sync,
    "httpx-async": _httpx_async,
    "requests": _requests,
    "urllib": _urllib,
    "aiohttp": _aiohttp,
}


def _headers_after_init(
    url: str, call: Callable[[str], None], disable: tuple[str, ...]
) -> dict[str, str]:
    _Recorder.seen.clear()
    try:
        with _controls.disabled(*disable):
            init_error_tracking(
                dsn=DUMMY_DSN,
                environment="prod",
                release="sha-test",
                service_name="unit",
                transport=MemoryTransport(),
            )
        call(url)
    finally:
        sentry_sdk.get_global_scope().set_client(None)
    assert len(_Recorder.seen) == 1
    return _Recorder.seen[0]


def _assert_no_trace_headers(headers: dict[str, str]) -> None:
    leaked = [h for h in TRACE_HEADERS if h in headers]
    assert not leaked, f"trace headers sent to vendor: {leaked}"


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_no_trace_headers_on_outbound_calls(server_url: str, client: str) -> None:
    _assert_no_trace_headers(_headers_after_init(server_url, CLIENTS[client], ()))


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_trace_header_test_fails_without_the_control(server_url: str, client: str) -> None:
    headers = _headers_after_init(server_url, CLIENTS[client], ("no_trace_propagation",))
    with pytest.raises(AssertionError):
        _assert_no_trace_headers(headers)


def test_option_is_an_empty_list() -> None:
    init_error_tracking(
        dsn=DUMMY_DSN,
        environment="prod",
        release="sha-test",
        service_name="unit",
        transport=MemoryTransport(),
    )
    try:
        assert sentry_sdk.get_client().options["trace_propagation_targets"] == []
    finally:
        sentry_sdk.get_global_scope().set_client(None)
