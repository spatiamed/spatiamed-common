import json
from typing import Any

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    AuthError,
    NotFound,
    TransientError,
    VendorRejected,
    WriteNotSupported,
)

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
ORDER = "3f0b8f0e-7d0c-4b8e-9a52-6c1d2f4e9a11"
# Synthetic order-type uuids: the tests do not depend on any install's values.
DRUG_TYPE = "0d0d0d0d-0000-4000-8000-0000000000d1"
LAB_TYPE = "0d0d0d0d-0000-4000-8000-0000000000a1"


def _client(**over: object) -> BahmniRecordClient:
    base: dict[str, object] = {
        "location_uuid": "loc-1",
        "identifier_type_uuid": "idtype",
        "identifier_source_uuid": "idsrc",
    }
    base.update(over)
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig(**base))  # type: ignore[arg-type]


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _order_body(
    *,
    type_name: str = "Drug Order",
    type_uuid: str = DRUG_TYPE,
    status: str | None = None,
    comment: str | None = None,
    accession: str | None = None,
    with_comment_key: bool = True,
    with_order_type: bool = True,
    voided: bool = False,
    action: str = "NEW",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "uuid": ORDER,
        "fulfillerStatus": status,
        "accessionNumber": accession,
        "voided": voided,
        "action": action,
    }
    if with_order_type:
        body["orderType"] = {"uuid": type_uuid, "name": type_name}
    if with_comment_key:
        body["fulfillerComment"] = comment
    return body


def _get_order(body: dict[str, Any] | None = None, status: int = 200) -> respx.Route:
    return respx.get(f"{REST}/order/{ORDER}").mock(
        return_value=httpx.Response(status, json=body if body is not None else _order_body())
    )


def _post(status: int = 201) -> respx.Route:
    return respx.post(f"{REST}/order/{ORDER}/fulfillerdetails").mock(
        return_value=httpx.Response(status)
    )


def _sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_posts_fulfiller_details() -> None:
    _login()
    get = _get_order()
    post = _post()
    await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    assert post.call_count == 1
    assert _sent(post) == {"fulfillerStatus": "COMPLETED", "fulfillerComment": "sm:dispensed"}
    # The pre-read asks for the fulfiller fields and the order type, nested group last.
    rep = get.calls.last.request.url.params["v"]
    assert rep.startswith("custom:(") and "fulfillerStatus" in rep
    assert "fulfillerComment" in rep
    assert "voided" in rep and "action" in rep
    assert rep.endswith("orderType:(uuid,name))")


@respx.mock
async def test_accession_key_absent_unless_passed() -> None:
    _login()
    _get_order()
    post = _post(200)
    await _client().set_fulfiller_status(ORDER, "IN_PROGRESS")
    body = _sent(post)
    # Never a null: a null could clear what the lab feed wrote.
    assert "accessionNumber" not in body
    assert body == {"fulfillerStatus": "IN_PROGRESS", "fulfillerComment": ""}


@respx.mock
async def test_accession_sent_for_drug_order_when_passed() -> None:
    _login()
    _get_order()
    post = _post(204)
    await _client().set_fulfiller_status(
        ORDER, "COMPLETED", comment="sm:dispensed", accession_number="SM2610250001"
    )
    assert _sent(post)["accessionNumber"] == "SM2610250001"


@respx.mock
async def test_accession_refused_for_lab_order() -> None:
    _login()
    _get_order(_order_body(type_name="Lab Order", type_uuid=LAB_TYPE))
    post = _post()
    with pytest.raises(ValueError, match="accession"):
        await _client().set_fulfiller_status(
            ORDER, "RECEIVED", comment="sm:collected", accession_number="SM2610250001"
        )
    assert post.call_count == 0


@respx.mock
async def test_lab_order_status_without_accession_posts() -> None:
    _login()
    _get_order(_order_body(type_name="Lab Order", type_uuid=LAB_TYPE, accession="enc-uuid"))
    post = _post()
    await _client(lab_order_type_uuid=LAB_TYPE).set_fulfiller_status(
        ORDER, "RECEIVED", comment="sm:collected"
    )
    assert _sent(post) == {"fulfillerStatus": "RECEIVED", "fulfillerComment": "sm:collected"}


@respx.mock
async def test_fulfiller_404_is_write_not_supported() -> None:
    _login()
    get = _get_order()
    post = _post(404)
    with pytest.raises(WriteNotSupported):
        await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    # Terminal: one read, one write, never a retry loop.
    assert get.call_count == 1
    assert post.call_count == 1


@respx.mock
async def test_missing_order_is_not_found_not_write_not_supported() -> None:
    _login()
    _get_order({"error": {"message": "Object not found"}}, status=404)
    post = _post(404)
    with pytest.raises(NotFound):
        await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    assert post.call_count == 0


@respx.mock
async def test_repeat_same_status_is_noop() -> None:
    _login()
    _get_order(_order_body(status="COMPLETED", comment="sm:dispensed"))
    post = _post()
    await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    assert post.call_count == 0


@respx.mock
async def test_same_status_new_comment_posts() -> None:
    _login()
    _get_order(_order_body(status="EXCEPTION", comment="sm:not_collected:patient_left"))
    post = _post()
    await _client().set_fulfiller_status(
        ORDER, "EXCEPTION", comment="sm:not_collected:sample_hemolysed"
    )
    assert post.call_count == 1


@respx.mock
async def test_comment_key_absent_on_read_posts() -> None:
    # A REST build that does not expose fulfillerComment: unknown, so write (harmless).
    _login()
    _get_order(_order_body(status="COMPLETED", with_comment_key=False))
    post = _post()
    # Even an empty comment: the record's comment is unknown, not "".
    await _client().set_fulfiller_status(ORDER, "COMPLETED")
    assert post.call_count == 1


@respx.mock
async def test_same_status_other_accession_posts() -> None:
    _login()
    _get_order(_order_body(status="COMPLETED", comment="sm:dispensed", accession="SM1"))
    post = _post()
    await _client().set_fulfiller_status(
        ORDER, "COMPLETED", comment="sm:dispensed", accession_number="SM2"
    )
    assert post.call_count == 1


@pytest.mark.parametrize(
    "comment",
    [
        "patient said thanks",
        "sm:dispensed today",
        "sm:dispensed ",
        "SM:DISPENSED",
        "sm:collected:extra",
        "sm:not_collected",
        "sm:not_collected:",
        "sm:not_collected:Patient Left",
        "sm:not_collected:" + "a" * 33,
        "sm:unknown",
    ],
)
@respx.mock
async def test_comment_is_code_not_free_text(comment: str) -> None:
    _login()
    get = _get_order()
    post = _post()
    with pytest.raises(ValueError, match="comment"):
        await _client().set_fulfiller_status(ORDER, "COMPLETED", comment=comment)
    # Refused before any I/O.
    assert get.call_count == 0
    assert post.call_count == 0


@pytest.mark.parametrize(
    "comment",
    ["", "sm:dispensed", "sm:partial", "sm:collected", "sm:not_collected:patient_left_2"],
)
@respx.mock
async def test_comment_codes_accepted(comment: str) -> None:
    _login()
    _get_order()
    post = _post()
    await _client().set_fulfiller_status(ORDER, "IN_PROGRESS", comment=comment)
    assert _sent(post)["fulfillerComment"] == comment


@respx.mock
async def test_unknown_status_refused_before_io() -> None:
    _login()
    get = _get_order()
    with pytest.raises(ValueError, match="status"):
        await _client().set_fulfiller_status(ORDER, "DONE", comment="sm:dispensed")  # type: ignore[arg-type]
    assert get.call_count == 0


@respx.mock
async def test_transient_and_auth_errors_propagate_without_retry() -> None:
    _login()
    _get_order()
    post = _post(503)
    with pytest.raises(TransientError):
        await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    assert post.call_count == 1
    respx.post(f"{REST}/order/{ORDER}/fulfillerdetails").mock(return_value=httpx.Response(403))
    with pytest.raises(AuthError):
        await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")


@pytest.mark.parametrize("bad", ["x/../patient", "a?v=full", "..", "", "a b", "x" * 39])
@respx.mock
async def test_order_uuid_checked_before_io(bad: str) -> None:
    # The uuid goes into the read and the write path: '/', '?' or '..' would reach
    # another REST resource.
    _login()
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="order_uuid"):
        await _client().set_fulfiller_status(bad, "COMPLETED", comment="sm:dispensed")
    assert route.call_count == 0


@respx.mock
async def test_accession_refused_when_order_type_absent() -> None:
    # Unknown kind: fail closed, the lab feed may own the field.
    _login()
    _get_order(_order_body(with_order_type=False))
    post = _post()
    with pytest.raises(ValueError, match="accession"):
        await _client().set_fulfiller_status(
            ORDER, "COMPLETED", comment="sm:dispensed", accession_number="SM2610250001"
        )
    assert post.call_count == 0


@pytest.mark.parametrize(
    ("type_name", "type_uuid", "config"),
    [
        # A site's custom lab type: neither stock name matches.
        ("Pathology Order", "0d0d0d0d-0000-4000-8000-0000000000c1", {}),
        # A configured lab type uuid that does not match the order's type.
        ("Lab Order", LAB_TYPE, {"lab_order_type_uuid": "0d0d0d0d-0000-4000-8000-0000000000b1"}),
    ],
)
@respx.mock
async def test_accession_refused_unless_drug_order(
    type_name: str, type_uuid: str, config: dict[str, str]
) -> None:
    _login()
    _get_order(_order_body(type_name=type_name, type_uuid=type_uuid))
    post = _post()
    with pytest.raises(ValueError, match="accession"):
        await _client(**config).set_fulfiller_status(
            ORDER, "RECEIVED", comment="sm:collected", accession_number="SM2610250001"
        )
    assert post.call_count == 0


@pytest.mark.parametrize("payload", [b"[]", b"", b'"order"'])
@respx.mock
async def test_non_object_read_is_vendor_rejected(payload: bytes) -> None:
    _login()
    respx.get(f"{REST}/order/{ORDER}").mock(return_value=httpx.Response(200, content=payload))
    post = _post()
    with pytest.raises(VendorRejected):
        await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    assert post.call_count == 0


@pytest.mark.parametrize(
    "over", [{"voided": True}, {"action": "DISCONTINUE"}, {"action": "discontinue"}]
)
@respx.mock
async def test_voided_or_discontinue_order_is_noop(over: dict[str, Any]) -> None:
    # Nothing to fulfil: a voided order is gone, a DISCONTINUE order only stops another.
    _login()
    _get_order(_order_body(**over))
    post = _post()
    await _client().set_fulfiller_status(ORDER, "COMPLETED", comment="sm:dispensed")
    assert post.call_count == 0
