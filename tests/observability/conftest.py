from __future__ import annotations

import json
import random
import string
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from sm_common.observability import _controls, init_error_tracking

DUMMY_DSN = "https://public@example.ingest.sentry.io/1"


class MemoryTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(json.loads(json.dumps(event, default=str)))

    def only_event(self) -> dict[str, Any]:
        assert len(self.events) == 1, f"expected 1 event, got {len(self.events)}"
        return self.events[0]

    def blob(self) -> str:
        return json.dumps(self.events)


Capture = Callable[..., Any]


@pytest.fixture
def capture() -> Iterator[Capture]:
    @contextmanager
    def _capture(disable: tuple[str, ...] = ()) -> Iterator[MemoryTransport]:
        transport = MemoryTransport()
        with _controls.disabled(*disable):
            init_error_tracking(
                dsn=DUMMY_DSN,
                environment="prod",
                release="sha-test",
                service_name="unit",
                transport=transport,
            )
            yield transport
            sentry_sdk.flush()

    yield _capture
    # sentry_sdk.init() with no DSN still installs an _Client whose is_active() is
    # True (client.py:747-752). set_client(None) installs a NonRecordingClient.
    sentry_sdk.get_global_scope().set_client(None)


# Sentinels are generated at runtime so they never appear in source context lines
# (include_source_context ships 5 lines around each frame) and never repeat
# (DedupeIntegration would swallow a repeat).
def nonce() -> str:
    # Letters only: a hex nonce occasionally holds a 10-digit run starting 6-9,
    # which the scrubber (correctly) redacts as a phone, making markers flaky.
    return "".join(random.choices(string.ascii_lowercase, k=10))


def fake_name() -> str:
    return f"Zebulon{nonce()} Quixote"


def fake_phone() -> str:
    return "9" + "".join(random.choices("0123456789", k=9))


def fake_aadhaar() -> str:
    d = "2" + "".join(random.choices("0123456789", k=11))
    return f"{d[:4]} {d[4:8]} {d[8:]}"


def fake_email() -> str:
    return f"pt{nonce()}@example.org"


def fake_jwt() -> str:
    return f"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI{nonce()}In0.sig{nonce()}"
