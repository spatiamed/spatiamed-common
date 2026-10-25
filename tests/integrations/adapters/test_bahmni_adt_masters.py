"""Ward, bed type and bed master-data creation (bed management 6.0.0 resources).

Synthetic fixtures only: random uuids, invented ward and bed names.
"""

import json

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    ConfigError,
    ConflictError,
    NotFound,
    TransientError,
    VendorRejected,
)
from sm_common.integrations.record_types import RecordBed, RecordWard

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
WARDS = f"{REST}/admissionLocation"
LOCATION = f"{REST}/location"
LOCATION_TAG = f"{REST}/locationtag"
BEDTYPE = f"{REST}/bedtype"
BED = f"{REST}/bed"
REQ = {"location_uuid": "L", "identifier_type_uuid": "T", "identifier_source_uuid": "S"}

PARENT = "3f1b6a2e-5c0d-4f7a-9e21-6b8d4c2a7f10"
OTHER_PARENT = "9a4e1c7b-2d3f-4b8a-8c65-1e0f7d3b9a42"
WARD = "c7d2e9f4-1a3b-4c5d-8e6f-7a8b9c0d1e2f"
OTHER_WARD = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
TAG_UUID = "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e"
NEW_BED_UUID = "d4e5f6a7-b8c9-4d0e-9f1a-2b3c4d5e6f7a"
OLD_BED_UUID = "e5f6a7b8-c9d0-4e1f-8a2b-3c4d5e6f7a8b"
BEDTYPE_UUID = "f6a7b8c9-d0e1-4f2a-9b3c-4d5e6f7a8b9c"


def _client() -> BahmniRecordClient:
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig.from_mapping(REQ))


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _ward_row(uuid: str, name: str, total: int = 0, occupied: int = 0) -> dict:
    return {"ward": {"uuid": uuid, "display": name}, "totalBeds": total, "occupiedBeds": occupied}


def _tag_present() -> respx.Route:
    """The "Admission Location" tag exists (and so does PARENT, create_ward's parent)."""
    respx.get(f"{LOCATION}/{PARENT}").mock(return_value=httpx.Response(200, json={"uuid": PARENT}))
    return respx.get(LOCATION_TAG).mock(
        return_value=httpx.Response(
            200, json={"results": [{"uuid": TAG_UUID, "name": "Admission Location"}]}
        )
    )


def _no_wards() -> respx.Route:
    """No live location holds the ward name (create_ward's lookup is a location search)."""
    return respx.get(LOCATION).mock(return_value=httpx.Response(200, json={"results": []}))


def _cell(row: int, col: int, bed: dict | None = None) -> dict:
    base = {
        "rowNumber": row,
        "columnNumber": col,
        "bedId": None,
        "bedUuid": None,
        "bedNumber": None,
        "status": None,
        "bedType": None,
        "patients": [],
    }
    return {**base, **(bed or {})}


def _layout(*cells: dict, rows: int = 2, cols: int = 2) -> dict:
    by_pos = {(c["rowNumber"], c["columnNumber"]): c for c in cells}
    layouts = [
        by_pos.get((r, c), _cell(r, c)) for r in range(1, rows + 1) for c in range(1, cols + 1)
    ]
    filled = sum(1 for c in layouts if c["bedId"] is not None)
    return {**_ward_row(WARD, "Test Ward", filled, 0), "bedLayouts": layouts}


def _existing_bed(row: int, col: int, number: str, bed_type: str = "standard") -> dict:
    return _cell(
        row,
        col,
        {
            "bedId": 41,
            "bedUuid": OLD_BED_UUID,
            "bedNumber": number,
            "status": "AVAILABLE",
            "bedType": {"uuid": BEDTYPE_UUID, "name": bed_type, "displayName": "Std"},
        },
    )


def _ward_full(body: dict) -> respx.Route:
    return respx.get(f"{WARDS}/{WARD}", params={"v": "full"}).mock(
        return_value=httpx.Response(200, json=body)
    )


def _location(
    uuid: str,
    name: str = "Test Ward",
    parent: str | None = PARENT,
    tags: tuple[str, ...] | list[str] = ("Admission Location",),
    retired: bool = False,
) -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "retired": retired,
        "parentLocation": {"uuid": parent} if parent else None,
        "tags": [{"name": t} for t in tags],
    }


def _dup_400() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "message": "Invalid Submission",
                "code": "webservices.rest.error.invalid.submission",
                "globalErrors": [],
                "fieldErrors": {
                    "name": [{"code": "location.duplicate.name", "message": "Name already in use"}]
                },
            }
        },
    )


def _reuse(display: str = "Test Ward", parent: str = PARENT) -> None:
    """A live ward of that name under ``parent``, found by location search."""
    respx.get(LOCATION).mock(
        return_value=httpx.Response(
            200, json={"results": [_location(WARD, name=display, parent=parent)]}
        )
    )


def _posts() -> list[httpx.Request]:
    return [c.request for c in respx.calls if c.request.method == "POST"]


# ─── create_ward ─────────────────────────────────────────────────────────────


@respx.mock
async def test_create_ward_two_calls_in_order() -> None:
    _login()
    _tag_present()
    _no_wards()
    create = respx.post(WARDS).mock(
        return_value=httpx.Response(201, json=_ward_row(WARD, "Test Ward"))
    )
    layout = respx.post(f"{WARDS}/{WARD}").mock(
        return_value=httpx.Response(200, json=_ward_row(WARD, "Test Ward"))
    )

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=3, columns=4
    )

    assert ward == RecordWard(uuid=WARD, name="Test Ward", total_beds=0, occupied_beds=0, beds=())
    posts = _posts()
    assert [str(p.url) for p in posts] == [WARDS, f"{WARDS}/{WARD}"]
    assert json.loads(create.calls[0].request.content) == {
        "name": "Test Ward",
        "parentLocationUuid": PARENT,
    }
    assert json.loads(layout.calls[0].request.content) == {"bedLayout": {"row": 3, "column": 4}}


@respx.mock
async def test_missing_admission_tag_config_error() -> None:
    # Bahmni answers a create without the tag with HTTP 500 (IllegalStateException), which
    # the client cannot tell from any other 500, so the tag is checked before writing.
    _login()
    tag = respx.get(LOCATION_TAG).mock(
        return_value=httpx.Response(
            200, json={"results": [{"uuid": TAG_UUID, "name": "Admission Location Old"}]}
        )
    )
    _no_wards()
    respx.post(WARDS).mock(return_value=httpx.Response(500))

    with pytest.raises(ConfigError, match="'Admission Location' location tag missing"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )

    assert tag.calls[0].request.url.params["q"] == "Admission Location"
    assert _posts() == []


@respx.mock
async def test_create_ward_retry_found_by_name_reapplies_layout_only() -> None:
    # First attempt created the location, then the layout POST failed: the retry must not
    # create a second ward (Bahmni would refuse the duplicate name with a 400 anyway).
    _login()
    _tag_present()
    respx.get(LOCATION).mock(
        return_value=httpx.Response(
            200,
            json={"results": [_location(OTHER_WARD, name="Test Ward 2"), _location(WARD)]},
        )
    )
    _ward_full({**_ward_row(WARD, "Test Ward"), "bedLayouts": []})
    create = respx.post(WARDS).mock(return_value=httpx.Response(201, json=_ward_row(WARD, "x")))
    layout = respx.post(f"{WARDS}/{WARD}").mock(
        return_value=httpx.Response(200, json=_ward_row(WARD, "Test Ward"))
    )

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=2, columns=2
    )

    assert ward.uuid == WARD
    assert create.call_count == 0
    assert layout.call_count == 1
    assert json.loads(layout.calls[0].request.content) == {"bedLayout": {"row": 2, "column": 2}}


@respx.mock
async def test_create_ward_name_under_other_parent_conflict() -> None:
    _login()
    _tag_present()
    respx.get(LOCATION).mock(
        return_value=httpx.Response(200, json={"results": [_location(WARD, parent=OTHER_PARENT)]})
    )

    with pytest.raises(ConflictError, match="ward_name_taken"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )
    assert _posts() == []


@respx.mock
async def test_create_ward_duplicate_name_400_conflict() -> None:
    # A non-ward location already holds the name: Bahmni's location validator says 400.
    # Body shape per webservices.rest RestUtil.wrapValidationErrorResponse.
    _login()
    _tag_present()
    _no_wards()
    respx.post(WARDS).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid Submission",
                    "code": "webservices.rest.error.invalid.submission",
                    "globalErrors": [],
                    "fieldErrors": {
                        "name": [
                            {"code": "location.duplicate.name", "message": "Name already in use"}
                        ]
                    },
                }
            },
        )
    )
    search = respx.get(LOCATION).mock(
        side_effect=[
            httpx.Response(200, json={"results": []}),
            httpx.Response(200, json={"results": [_location(WARD, tags=["Visit Location"])]}),
        ]
    )
    layout = respx.post(f"{WARDS}/{WARD}")

    with pytest.raises(ConflictError, match="ward_name_taken"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )
    assert layout.call_count == 0
    assert [str(p.url) for p in _posts()] == [WARDS]
    assert search.calls[1].request.url.params["q"] == "Test Ward"


@respx.mock
async def test_create_ward_response_without_uuid_landed() -> None:
    _login()
    _tag_present()
    _no_wards()
    respx.post(WARDS).mock(return_value=httpx.Response(201, json={"totalBeds": 0}))

    with pytest.raises(VendorRejected) as err:
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )
    assert err.value.landed is True


@respx.mock
async def test_create_ward_layout_route_404_not_found() -> None:
    _login()
    _tag_present()
    _no_wards()
    respx.post(WARDS).mock(return_value=httpx.Response(201, json=_ward_row(WARD, "Test Ward")))
    respx.post(f"{WARDS}/{WARD}").mock(return_value=httpx.Response(404))

    with pytest.raises(NotFound):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "  ", "parent_location_uuid": PARENT, "rows": 1, "columns": 1},
        {"name": "x" * 256, "parent_location_uuid": PARENT, "rows": 1, "columns": 1},
        {"name": "Test Ward", "parent_location_uuid": "../session", "rows": 1, "columns": 1},
        {"name": "Test Ward", "parent_location_uuid": PARENT, "rows": 0, "columns": 1},
        {"name": "Test Ward", "parent_location_uuid": PARENT, "rows": 1, "columns": True},
        {"name": "Test Ward", "parent_location_uuid": PARENT, "rows": 51, "columns": 1},
        {"name": "Test Ward", "parent_location_uuid": PARENT, "rows": 1, "columns": 51},
    ],
)
@respx.mock
async def test_create_ward_bad_args_value_error_before_http(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        await _client().create_ward(**kwargs)
    assert respx.calls.call_count == 0


# ─── ensure_bed_type ─────────────────────────────────────────────────────────


@respx.mock
async def test_ensure_bed_type_idempotent() -> None:
    _login()
    state: list[dict] = []

    def search(request: httpx.Request) -> httpx.Response:
        name = request.url.params["name"]
        return httpx.Response(200, json={"results": [t for t in state if t["name"] == name]})

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        row = {"uuid": BEDTYPE_UUID, "name": body["name"], "displayName": body["displayName"]}
        state.append(row)
        return httpx.Response(201, json=row)

    respx.get(BEDTYPE).mock(side_effect=search)
    post = respx.post(BEDTYPE).mock(side_effect=create)
    client = _client()

    assert await client.ensure_bed_type(name="icu", display_name="ICU") == "icu"
    assert await client.ensure_bed_type(name="icu", display_name="ICU") == "icu"

    assert post.call_count == 1
    assert json.loads(post.calls[0].request.content) == {"name": "icu", "displayName": "ICU"}


@respx.mock
async def test_ensure_bed_type_existing_returns_server_name_no_post() -> None:
    _login()
    respx.get(BEDTYPE).mock(
        return_value=httpx.Response(
            200, json={"results": [{"uuid": BEDTYPE_UUID, "name": "icu", "displayName": "ICU"}]}
        )
    )
    post = respx.post(BEDTYPE)

    assert await _client().ensure_bed_type(name="icu", display_name="Intensive") == "icu"
    assert post.call_count == 0


@respx.mock
async def test_ensure_bed_type_route_404_not_found() -> None:
    _login()
    respx.get(BEDTYPE).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().ensure_bed_type(name="icu", display_name="ICU")


@pytest.mark.parametrize(
    ("name", "display"), [("", "ICU"), ("icu", " "), ("icu", "X" * 11), ("x" * 256, "ICU")]
)
@respx.mock
async def test_ensure_bed_type_bad_args_value_error_before_http(name: str, display: str) -> None:
    with pytest.raises(ValueError):
        await _client().ensure_bed_type(name=name, display_name=display)
    assert respx.calls.call_count == 0


# ─── add_bed ─────────────────────────────────────────────────────────────────


def _bed_response(number: str = "TW-1", row: int = 1, col: int = 2) -> dict:
    return {
        "id": 42,
        "uuid": NEW_BED_UUID,
        "bedNumber": number,
        "bedType": {"uuid": BEDTYPE_UUID, "name": "standard", "displayName": "Std"},
        "row": row,
        "column": col,
        "status": "AVAILABLE",
    }


@respx.mock
async def test_add_bed_posts_bed_with_location() -> None:
    _login()
    _ward_full(_layout(_existing_bed(1, 1, "TW-0")))
    post = respx.post(BED).mock(return_value=httpx.Response(201, json=_bed_response()))

    bed = await _client().add_bed(
        ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
    )

    assert bed == RecordBed(
        bed_id=42,
        bed_uuid=NEW_BED_UUID,
        bed_number="TW-1",
        bed_type="standard",
        status="AVAILABLE",
        row=1,
        column=2,
        patient_uuids=(),
    )
    assert json.loads(post.calls[0].request.content) == {
        "bedNumber": "TW-1",
        "bedType": "standard",
        "row": 1,
        "column": 2,
        "locationUuid": WARD,
    }


@respx.mock
async def test_add_bed_cell_taken_conflict() -> None:
    # Bahmni saves the bed BEFORE it maps the cell, so its own 400 would leave an
    # unmapped bed behind: the occupied cell is refused before any POST.
    _login()
    _ward_full(_layout(_existing_bed(1, 2, "TW-9")))
    post = respx.post(BED)

    with pytest.raises(ConflictError, match="^bed_cell_taken$"):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
        )
    assert post.call_count == 0


@respx.mock
async def test_add_bed_cell_taken_race_400_conflict() -> None:
    _login()
    _ward_full(_layout())
    respx.post(BED).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Already bed assign to give row & column"}}
        )
    )

    with pytest.raises(ConflictError, match="bed_cell_taken_orphaned"):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
        )


@respx.mock
async def test_add_bed_retry_same_bed_in_cell_no_post() -> None:
    _login()
    _ward_full(_layout(_existing_bed(1, 2, "TW-1")))
    post = respx.post(BED)

    bed = await _client().add_bed(
        ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
    )

    assert (bed.bed_uuid, bed.bed_number, bed.row, bed.column) == (OLD_BED_UUID, "TW-1", 1, 2)
    assert post.call_count == 0


@pytest.mark.parametrize("stored_type", [None, "icu"], ids=["type_cleared", "other_type"])
@respx.mock
async def test_add_bed_retry_same_number_in_cell_ignores_bed_type(stored_type: str | None) -> None:
    # bedmanagement 6.0.0 clears a bed's type (bed_type_id NULL) whenever a patient leaves
    # it, so the cell + number identify the bed; its type is not a reliable key.
    _login()
    cell = _existing_bed(1, 2, "TW-1")
    cell["bedType"] = (
        None
        if stored_type is None
        else {"uuid": BEDTYPE_UUID, "name": stored_type, "displayName": "x"}
    )
    _ward_full(_layout(cell))

    bed = await _client().add_bed(
        ward_uuid=WARD, bed_number="tw-1", bed_type="standard", row=1, column=2
    )
    assert (bed.bed_uuid, bed.row, bed.column) == (OLD_BED_UUID, 1, 2)
    assert _posts() == []


@respx.mock
async def test_add_bed_number_elsewhere_in_ward_conflict() -> None:
    _login()
    _ward_full(_layout(_existing_bed(2, 2, "TW-1")))
    with pytest.raises(ConflictError, match="bed_number_taken"):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
        )
    assert _posts() == []


@respx.mock
async def test_add_bed_outside_layout_conflict() -> None:
    # Bahmni would silently map a cell outside the grid.
    _login()
    _ward_full(_layout(rows=2, cols=2))
    with pytest.raises(ConflictError, match="bed_cell_outside_layout"):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=3, column=1
        )
    assert _posts() == []


@respx.mock
async def test_add_bed_unknown_ward_not_found() -> None:
    _login()
    respx.get(f"{WARDS}/{WARD}").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCATION}/{WARD}").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=1
        )
    assert _posts() == []


@respx.mock
async def test_add_bed_unknown_type_400_vendor_rejected() -> None:
    _login()
    _ward_full(_layout())
    respx.post(BED).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Invalid bed type name"}})
    )
    with pytest.raises(VendorRejected) as err:
        await _client().add_bed(ward_uuid=WARD, bed_number="TW-1", bed_type="nope", row=1, column=1)
    assert not isinstance(err.value, ConflictError)


@respx.mock
async def test_add_bed_response_without_id_landed() -> None:
    _login()
    _ward_full(_layout())
    respx.post(BED).mock(return_value=httpx.Response(201, json={"bedNumber": "TW-1"}))
    with pytest.raises(VendorRejected) as err:
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=1
        )
    assert err.value.landed is True


@respx.mock
async def test_add_bed_server_error_transient() -> None:
    _login()
    _ward_full(_layout())
    respx.post(BED).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientError):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=1
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ward_uuid": "a/b", "bed_number": "TW-1", "bed_type": "standard", "row": 1, "column": 1},
        {"ward_uuid": WARD, "bed_number": " ", "bed_type": "standard", "row": 1, "column": 1},
        {"ward_uuid": WARD, "bed_number": "B" * 51, "bed_type": "standard", "row": 1, "column": 1},
        # Bahmni's bed.bed_number column is 10 characters wide.
        {"ward_uuid": WARD, "bed_number": "B" * 11, "bed_type": "standard", "row": 1, "column": 1},
        {"ward_uuid": WARD, "bed_number": "TW-1", "bed_type": "", "row": 1, "column": 1},
        {"ward_uuid": WARD, "bed_number": "TW-1", "bed_type": "standard", "row": 1, "column": 0},
        {
            "ward_uuid": WARD,
            "bed_number": "TW-1",
            "bed_type": "standard",
            "row": False,
            "column": 1,
        },
    ],
)
@respx.mock
async def test_add_bed_bad_args_value_error_before_http(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        await _client().add_bed(**kwargs)
    assert respx.calls.call_count == 0


# ─── Fix round 1 ─────────────────────────────────────────────────────────────


@respx.mock
async def test_create_ward_reuse_shrink_blocked_before_write() -> None:
    # A smaller grid would displace a bed: Bahmni's 500 never heals, so refuse first.
    _login()
    _tag_present()
    _reuse()
    _ward_full(_layout(_existing_bed(3, 1, "TW-31"), rows=3, cols=2))

    with pytest.raises(ConflictError, match="ward_layout_shrink_blocked"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=2, columns=2
        )
    assert _posts() == []


@respx.mock
async def test_create_ward_reuse_same_grid_no_post_case_insensitive_name() -> None:
    _login()
    _tag_present()
    _reuse(display="TEST WARD")
    _ward_full(_layout(_existing_bed(1, 1, "TW-11"), rows=2, cols=3))

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=2, columns=3
    )

    assert ward.uuid == WARD
    assert [b.bed_number for b in ward.beds] == ["TW-11"]
    assert _posts() == []


@respx.mock
async def test_create_ward_reuse_parent_uuid_case_insensitive() -> None:
    _login()
    _tag_present()
    _reuse(parent=PARENT.upper())
    _ward_full(_layout(rows=1, cols=1))

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
    )
    assert ward.uuid == WARD
    assert _posts() == []


@respx.mock
async def test_create_ward_reuse_grows_layout() -> None:
    _login()
    _tag_present()
    _reuse()
    _ward_full(_layout(_existing_bed(1, 1, "TW-11"), rows=1, cols=1))
    layout = respx.post(f"{WARDS}/{WARD}").mock(
        return_value=httpx.Response(200, json=_ward_row(WARD, "Test Ward", 1, 0))
    )

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=2, columns=2
    )

    assert json.loads(layout.calls[0].request.content) == {"bedLayout": {"row": 2, "column": 2}}
    assert ward.total_beds == 1
    assert [b.bed_number for b in ward.beds] == ["TW-11"]


@respx.mock
async def test_admission_tag_name_case_insensitive() -> None:
    _login()
    respx.get(f"{LOCATION}/{PARENT}").mock(return_value=httpx.Response(200, json={"uuid": PARENT}))
    respx.get(LOCATION_TAG).mock(
        return_value=httpx.Response(
            200, json={"results": [{"uuid": TAG_UUID, "name": "admission location"}]}
        )
    )
    _no_wards()
    respx.post(WARDS).mock(return_value=httpx.Response(201, json=_ward_row(WARD, "Test Ward")))
    respx.post(f"{WARDS}/{WARD}").mock(
        return_value=httpx.Response(200, json=_ward_row(WARD, "Test Ward"))
    )

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
    )
    assert ward.uuid == WARD


@respx.mock
async def test_create_ward_duplicate_400_falls_back_to_location_search() -> None:
    # A sub-ward (or a ward with a localized display) is missing from list_wards: the
    # retry of a create that landed gets the duplicate-name 400 and must reuse that ward.
    _login()
    _tag_present()
    _no_wards()
    create = respx.post(WARDS).mock(return_value=_dup_400())
    respx.get(LOCATION).mock(
        side_effect=[
            # A create racing ours lands between the lookup and the POST.
            httpx.Response(200, json={"results": []}),
            httpx.Response(
                200,
                json={
                    "results": [
                        _location(OTHER_WARD, retired=True),
                        _location(WARD, name="test ward"),
                    ]
                },
            ),
        ]
    )
    _ward_full(_layout(rows=1, cols=1))

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
    )

    assert ward.uuid == WARD
    assert create.call_count == 1
    assert len(_posts()) == 1


@respx.mock
async def test_create_ward_unknown_parent_not_found() -> None:
    _login()
    _tag_present()
    _no_wards()
    respx.post(WARDS).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Parent location not exist"}})
    )
    with pytest.raises(NotFound, match="parent"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )


@respx.mock
async def test_create_ward_create_route_404_not_found() -> None:
    _login()
    _tag_present()
    _no_wards()
    respx.post(WARDS).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )


@pytest.mark.parametrize(
    "body", [{"totalBeds": 0}, _ward_row(OTHER_WARD, "Other Ward")], ids=["malformed", "other"]
)
@respx.mock
async def test_create_ward_bad_layout_response_landed(body: dict) -> None:
    _login()
    _tag_present()
    _no_wards()
    respx.post(WARDS).mock(return_value=httpx.Response(201, json=_ward_row(WARD, "Test Ward")))
    respx.post(f"{WARDS}/{WARD}").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(VendorRejected) as err:
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )
    assert err.value.landed is True


@respx.mock
async def test_ensure_bed_type_reuses_collation_match() -> None:
    # Bahmni's name lookup follows the DB collation; bed create uses the same lookup.
    _login()
    respx.get(BEDTYPE).mock(
        return_value=httpx.Response(
            200, json={"results": [{"uuid": BEDTYPE_UUID, "name": "General", "displayName": "Gen"}]}
        )
    )
    post = respx.post(BEDTYPE)

    assert await _client().ensure_bed_type(name="general", display_name="Gen") == "General"
    assert post.call_count == 0


@respx.mock
async def test_add_bed_retry_type_case_insensitive() -> None:
    _login()
    _ward_full(_layout(_existing_bed(1, 2, "TW-1", bed_type="Standard")))

    bed = await _client().add_bed(
        ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
    )
    assert bed.bed_uuid == OLD_BED_UUID
    assert _posts() == []


@respx.mock
async def test_add_bed_number_taken_case_insensitive() -> None:
    _login()
    _ward_full(_layout(_existing_bed(2, 2, "tw-1")))
    with pytest.raises(ConflictError, match="bed_number_taken"):
        await _client().add_bed(
            ward_uuid=WARD, bed_number="TW-1", bed_type="standard", row=1, column=2
        )
    assert _posts() == []


# ─── Duplicate names: location search before the create, and after a 500 ─────
# OpenMRS keeps location names unique across all live locations. Bahmni's duplicate
# check answers the create with HTTP 500 (a generic HTML error page), not 400, so the
# name is looked up by location search before the POST and again after a 500.


def _search(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"results": list(rows)})


@respx.mock
async def test_create_ward_precheck_name_held_by_non_ward_conflict_no_post() -> None:
    _login()
    _tag_present()
    _no_wards()
    search = respx.get(LOCATION).mock(
        return_value=_search(_location(OTHER_WARD, name="TEST WARD", tags=["Visit Location"]))
    )
    create = respx.post(WARDS).mock(return_value=httpx.Response(500))

    with pytest.raises(ConflictError, match="^ward_name_taken$"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )
    assert create.call_count == 0
    assert search.calls[0].request.url.params["q"] == "Test Ward"


@respx.mock
async def test_create_ward_precheck_reuses_ward_missing_from_ward_list() -> None:
    # A sub-ward (or a ward with a localized display) is not in admissionLocation's list.
    _login()
    _tag_present()
    _no_wards()
    respx.get(LOCATION).mock(
        return_value=_search(
            _location(OTHER_WARD, retired=True),
            _location(WARD, name="test ward", parent=PARENT.upper()),
        )
    )
    create = respx.post(WARDS).mock(return_value=httpx.Response(500))
    _ward_full(_layout(rows=1, cols=1))

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
    )
    assert ward.uuid == WARD
    assert create.call_count == 0
    assert _posts() == []


@respx.mock
async def test_create_ward_precheck_follows_search_pages() -> None:
    # The search is a name-prefix match: the exact name can sit past the first page.
    _login()
    _tag_present()
    _no_wards()
    first = {
        "results": [_location(OTHER_WARD, name="Test Ward 2")],
        "links": [{"rel": "next", "uri": f"{LOCATION}?q=Test+Ward&startIndex=1"}],
    }
    route = respx.get(LOCATION).mock(
        side_effect=[httpx.Response(200, json=first), _search(_location(WARD))]
    )
    create = respx.post(WARDS).mock(return_value=httpx.Response(500))
    _ward_full(_layout(rows=1, cols=1))

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
    )
    assert ward.uuid == WARD
    assert create.call_count == 0
    assert route.calls[1].request.url.params["startIndex"] == "1"


@respx.mock
async def test_create_ward_500_then_found_ours_reuses() -> None:
    # The create landed (or raced another create of ours) but answered 500.
    _login()
    _tag_present()
    _no_wards()
    respx.get(LOCATION).mock(side_effect=[_search(), _search(_location(WARD))])
    create = respx.post(WARDS).mock(return_value=httpx.Response(500))
    _ward_full(_layout(rows=2, cols=2))

    ward = await _client().create_ward(
        name="Test Ward", parent_location_uuid=PARENT, rows=2, columns=2
    )
    assert ward.uuid == WARD
    assert create.call_count == 1
    assert len(_posts()) == 1


@respx.mock
async def test_create_ward_500_then_found_other_location_conflict() -> None:
    _login()
    _tag_present()
    _no_wards()
    respx.get(LOCATION).mock(
        side_effect=[_search(), _search(_location(OTHER_WARD, parent=OTHER_PARENT))]
    )
    respx.post(WARDS).mock(return_value=httpx.Response(500))

    with pytest.raises(ConflictError, match="^ward_name_taken$"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )


@respx.mock
async def test_create_ward_500_with_no_same_name_location_stays_transient() -> None:
    # Nothing holds the name: a real server fault, not a duplicate; never ward_name_taken.
    _login()
    _tag_present()
    _no_wards()
    respx.get(LOCATION).mock(side_effect=[_search(), _search(_location(OTHER_WARD, "Other"))])
    respx.post(WARDS).mock(return_value=httpx.Response(500))

    with pytest.raises(TransientError):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )


@respx.mock
async def test_create_ward_unknown_parent_not_found_before_any_write() -> None:
    # An unknown parent must not depend on how Bahmni refuses the create (400 or 500).
    _login()
    _tag_present()
    _no_wards()
    respx.get(f"{LOCATION}/{PARENT}").mock(return_value=httpx.Response(404))
    create = respx.post(WARDS).mock(return_value=httpx.Response(500))
    with pytest.raises(NotFound, match="parent"):
        await _client().create_ward(
            name="Test Ward", parent_location_uuid=PARENT, rows=1, columns=1
        )
    assert create.call_count == 0
