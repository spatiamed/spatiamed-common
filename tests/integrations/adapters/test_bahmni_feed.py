import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni import BahmniAdapter
from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.exceptions import TransientError

FIX = Path(__file__).parent.parent / "fixtures" / "bahmni"
BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
FEED = f"{BASE}/openmrs/ws/atomfeed/appointments"
SEARCH = f"{REST}/appointments/search"
U1 = "aaaaaaaa-0000-4000-8000-000000000001"
U2 = "aaaaaaaa-0000-4000-8000-000000000002"
U3 = "aaaaaaaa-0000-4000-8000-000000000003"
U4 = "aaaaaaaa-0000-4000-8000-000000000004"
UNTIL = date(2026, 11, 1)


def _adapter() -> BahmniAdapter:
    config = BahmniConfig(location_uuid="L", identifier_type_uuid="T", identifier_source_uuid="S")
    return BahmniAdapter(
        http=BahmniHttp(BASE, "u", "p"), config=config, hash_salt="s", transport_key="k"
    )


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _appt(uuid: str = U1, **over: object) -> dict:  # type: ignore[type-arg]
    body = json.loads((FIX / "appointment_get.json").read_text())
    body["uuid"] = uuid
    body.update(over)
    return body  # type: ignore[no-any-return]


def _serve_appts() -> None:
    for uuid in (U1, U2, U3, U4):
        respx.get(f"{REST}/appointments/{uuid}").mock(
            return_value=httpx.Response(200, json=_appt(uuid))
        )


def _page(entries: list[tuple[str, str]], prev: str | None = None, descending: bool = False) -> str:
    """entries: (atom id, appointment uuid) in document order; ``updated`` rises by default."""
    links = f'<link rel="prev-archive" href="{prev}"/>' if prev else ""
    body = "".join(
        f"<entry><id>{eid}</id><updated>2026-10-25T05:{(len(entries) - i if descending else i):02d}:00Z</updated>"
        f'<content type="application/vnd.atomfeed+xml"><![CDATA[{REST}/appointments/{u}]]>'
        "</content></entry>"
        for i, (eid, u) in enumerate(entries)
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{links}{body}</feed>'


def _feed(name: str, text: str) -> respx.Route:
    return respx.get(f"{FEED}/{name}").mock(return_value=httpx.Response(200, text=text))


@respx.mock
async def test_feed_uses_plural_url_and_resolves_entries():
    _login()
    recent = _feed("recent", (FIX / "feed_recent.xml").read_text())
    _serve_appts()
    got, cursor = await _adapter().list_appointments_modified_since("", UNTIL)
    assert recent.called
    assert cursor == "tag:atomfeed.example:6"
    # recent page only: U2, U3, U1 in feed order, deduped
    assert [a.appointment_id for a in got] == [U2, U3, U1]
    a = got[0]
    assert a.slot_start == datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    assert a.slot_duration_min == 15
    assert a.hms_version == 1792901000000
    assert a.doctor_external_id == "22222222-bbbb-4bbb-8bbb-000000000002"
    assert a.department_external_id == "33333333-cccc-4ccc-8ccc-000000000003"
    assert a.payer_type == "CASH"
    assert a.reason_text == "fever"
    assert a.status == "Scheduled"
    assert a.patient.mrn == "GAN200001"
    assert a.patient.resource_id == "11111111-aaaa-4aaa-8aaa-000000000001"


@respx.mock
async def test_feed_first_run_recent_page_only():
    _login()
    _feed("recent", (FIX / "feed_recent.xml").read_text())
    archive = _feed("1", (FIX / "feed_archive_1.xml").read_text())
    _serve_appts()
    await _adapter().list_appointments_modified_since("", UNTIL)
    assert not archive.called


@respx.mock
async def test_feed_legacy_iso_cursor_treated_as_first_run():
    _login()
    _feed("recent", (FIX / "feed_recent.xml").read_text())
    archive = _feed("1", (FIX / "feed_archive_1.xml").read_text())
    _serve_appts()
    got, _ = await _adapter().list_appointments_modified_since("2026-05-06T10:00:00Z", UNTIL)
    assert len(got) == 3
    assert not archive.called


@respx.mock
async def test_feed_recent_page_after_cursor_only():
    _login()
    _feed("recent", (FIX / "feed_recent.xml").read_text())
    archive = _feed("1", (FIX / "feed_archive_1.xml").read_text())
    _serve_appts()
    got, cursor = await _adapter().list_appointments_modified_since("tag:atomfeed.example:5", UNTIL)
    assert [a.appointment_id for a in got] == [U1]
    assert cursor == "tag:atomfeed.example:6"
    assert not archive.called


@respx.mock
async def test_feed_no_new_entries_keeps_cursor():
    _login()
    _feed("recent", (FIX / "feed_recent.xml").read_text())
    got, cursor = await _adapter().list_appointments_modified_since("tag:atomfeed.example:6", UNTIL)
    assert got == []
    assert cursor == "tag:atomfeed.example:6"


@respx.mock
async def test_feed_walks_archive_to_cursor():
    _login()
    _feed("recent", (FIX / "feed_recent.xml").read_text())
    _feed("1", (FIX / "feed_archive_1.xml").read_text())
    _serve_appts()
    got, cursor = await _adapter().list_appointments_modified_since("tag:atomfeed.example:1", UNTIL)
    # after id 1, oldest first: archive 2 (U2), 3 (U4), then recent 4 (U2), 5 (U3), 6 (U1).
    # Dedupe keeps the last occurrence: U4, U2, U3, U1.
    assert [a.appointment_id for a in got] == [U4, U2, U3, U1]
    assert cursor == "tag:atomfeed.example:6"


@respx.mock
async def test_feed_cursor_lost_is_bounded(caplog: pytest.LogCaptureFixture):
    _login()
    # recent + 30 archives, each one entry; none holds the cursor
    _feed("recent", _page([("e-recent", U1)], prev=f"{FEED}/a1"))
    routes = []
    for n in range(1, 31):
        nxt = f"{FEED}/a{n + 1}" if n < 30 else None
        routes.append(_feed(f"a{n}", _page([(f"e-a{n}", U2)], prev=nxt)))
    _serve_appts()
    with caplog.at_level(logging.WARNING):
        got, cursor = await _adapter().list_appointments_modified_since("tag:gone", UNTIL)
    assert sum(r.call_count for r in routes) == 20
    assert not routes[20].called
    assert cursor == "e-recent"
    assert {a.appointment_id for a in got} == {U1, U2}
    assert "cursor_lost" in caplog.text


@respx.mock
async def test_feed_cursor_lost_at_end_of_archive_chain(caplog: pytest.LogCaptureFixture):
    _login()
    _feed("recent", _page([("e2", U1)], prev=f"{FEED}/a1"))
    _feed("a1", _page([("e1", U2)]))
    _serve_appts()
    with caplog.at_level(logging.WARNING):
        got, _ = await _adapter().list_appointments_modified_since("tag:gone", UNTIL)
    assert len(got) == 2
    assert "cursor_lost" in caplog.text


@respx.mock
async def test_feed_prev_archive_loop_terminates():
    _login()
    _feed("recent", _page([("e2", U1)], prev=f"{FEED}/a1"))
    _feed("a1", _page([("e1", U2)], prev=f"{FEED}/a1"))
    _serve_appts()
    got, _ = await _adapter().list_appointments_modified_since("tag:gone", UNTIL)
    assert len(got) == 2


@respx.mock
async def test_feed_dedupes_cancel_event():
    _login()
    _feed("recent", _page([("e1", U1), ("e2", U1)]))
    get = respx.get(f"{REST}/appointments/{U1}").mock(
        return_value=httpx.Response(200, json=_appt(U1, status="Cancelled"))
    )
    got, cursor = await _adapter().list_appointments_modified_since("", UNTIL)
    assert len(got) == 1
    assert got[0].status == "Cancelled"
    assert get.call_count == 1
    assert cursor == "e2"


@respx.mock
async def test_feed_entry_404_skipped_and_logged(caplog: pytest.LogCaptureFixture):
    _login()
    _feed("recent", _page([("e1", U1), ("e2", U2)]))
    respx.get(f"{REST}/appointments/{U1}").mock(return_value=httpx.Response(404))
    respx.get(f"{REST}/appointments/{U2}").mock(return_value=httpx.Response(200, json=_appt(U2)))
    with caplog.at_level(logging.WARNING):
        got, cursor = await _adapter().list_appointments_modified_since("", UNTIL)
    assert [a.appointment_id for a in got] == [U2]
    assert cursor == "e2"
    assert "feed_entry_gone" in caplog.text


@respx.mock
async def test_feed_descending_page_is_processed_oldest_first():
    _login()
    # a page that lists the newest entry first
    _feed("recent", _page([("e1", U1), ("e2", U2), ("e3", U3)], descending=True))
    _serve_appts()
    got, cursor = await _adapter().list_appointments_modified_since("e3", UNTIL)
    assert [a.appointment_id for a in got] == [U2, U1]
    assert cursor == "e1"


@respx.mock
async def test_epoch_ms_parsed_not_iso():
    _login()
    _feed("recent", _page([("e1", U1)]))
    respx.get(f"{REST}/appointments/{U1}").mock(
        return_value=httpx.Response(
            200, json=_appt(U1, startDateTime=1792904400000, endDateTime=1792906200000)
        )
    )
    got, _ = await _adapter().list_appointments_modified_since("", UNTIL)
    assert got[0].slot_start == datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    assert got[0].slot_start.tzinfo is not None
    assert got[0].slot_duration_min == 30


@respx.mock
async def test_recent_bookings_uses_search_and_epoch():
    _login()
    search = respx.post(SEARCH).mock(
        return_value=httpx.Response(
            200,
            json=[_appt(U1, dateChanged=1792901000000), _appt(U2, dateChanged=None)],
        )
    )
    got = await _adapter().fetch_recent_bookings(uuid4(), 60)
    assert search.call_count == 1
    sent = json.loads(search.calls[0].request.content)
    start = datetime.fromisoformat(sent["startDate"])
    end = datetime.fromisoformat(sent["endDate"])
    assert end - start == timedelta(minutes=60, days=1)
    assert [b.appointment_id for b in got] == [U1, U2]
    assert got[0].updated_at == datetime(2026, 10, 25, 4, 3, 20, tzinfo=timezone.utc)
    assert got[1].updated_at == datetime(
        2026, 10, 25, 3, 46, 40, tzinfo=timezone.utc
    )  # dateCreated
    assert got[0].slot_start == datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    assert got[0].doctor_external_id == "22222222-bbbb-4bbb-8bbb-000000000002"


def _rows(n: int, prefix: str) -> list[dict]:  # type: ignore[type-arg]
    return [_appt(f"{prefix}-{i}") for i in range(n)]


@respx.mock
async def test_search_window_bisects_past_50_cap():
    _login()
    answers = iter([_rows(50, "full"), _rows(30, "left"), _rows(25, "right")])
    respx.post(SEARCH).mock(side_effect=lambda req: httpx.Response(200, json=next(answers)))
    start = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    rows, truncated = await _adapter()._search_window(start, start + timedelta(hours=1))
    assert not truncated
    assert len({r["uuid"] for r in rows}) == 55


@respx.mock
async def test_search_window_truncated_at_one_minute():
    _login()
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=_rows(50, "full")))
    start = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    rows, truncated = await _adapter()._search_window(start, start + timedelta(minutes=1))
    assert truncated
    assert route.call_count == 1
    assert len(rows) == 50


@respx.mock
async def test_day_list_bisects_past_50_cap():
    _login()

    # a busy day: any window over 70 minutes is full (50 rows), smaller ones are not
    def answer(req: httpx.Request) -> httpx.Response:
        sent = json.loads(req.content)
        span = datetime.fromisoformat(sent["endDate"]) - datetime.fromisoformat(sent["startDate"])
        if span > timedelta(minutes=70):
            return httpx.Response(200, json=_rows(50, "capped"))
        return httpx.Response(200, json=[_appt(f"leaf-{sent['startDate']}")])

    respx.post(SEARCH).mock(side_effect=answer)
    got = await _adapter().fetch_recent_bookings(uuid4(), 60)
    assert len(got) == 32
    assert not any(b.appointment_id.startswith("capped") for b in got)
