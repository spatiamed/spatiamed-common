import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from sm_common.integrations.adapters.bahmni_config import BahmniConfig
from sm_common.integrations.adapters.bahmni_http import BahmniHttp
from sm_common.integrations.adapters.bahmni_record import BahmniRecordClient
from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    DocumentFilingFailed,
    VendorRejected,
    WriteNotSupported,
)
from sm_common.integrations.record_types import RecordDocumentInput, RecordDocumentResult

BASE = "https://bahmni.example.test"
REST = f"{BASE}/openmrs/ws/rest/v1"
UPLOAD = f"{REST}/bahmnicore/visitDocument/uploadDocument"
FILE = f"{REST}/bahmnicore/visitDocument"
AT = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)
AT_MS = 1790829000000


def _client(**over: object) -> BahmniRecordClient:
    base: dict[str, object] = {
        "location_uuid": "loc-1",
        "identifier_type_uuid": "idtype",
        "identifier_source_uuid": "idsrc",
        "document_visit_type_uuid": "vt-doc",
        "document_encounter_type_uuid": "et-doc",
        "document_type_concepts": {"lab_report": "c-lab"},
    }
    base.update(over)
    return BahmniRecordClient(BahmniHttp(BASE, "u", "p"), BahmniConfig(**base))  # type: ignore[arg-type]


def _login() -> None:
    respx.get(f"{REST}/session").mock(
        return_value=httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "JSESSIONID=abc"}
        )
    )


def _doc(doc_type: str = "lab_report", mime: str = "application/pdf") -> RecordDocumentInput:
    return RecordDocumentInput("p-1", b"%PDF-x", mime, doc_type, "prov-1", AT)  # type: ignore[arg-type]


@respx.mock
async def test_upload_document_two_calls_in_order() -> None:
    _login()
    up = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "2026/f.pdf"}))
    fl = respx.post(FILE).mock(
        return_value=httpx.Response(200, json={"visitUuid": "v-1", "encounterUuid": "e-1"})
    )
    out = await _client().upload_document(_doc())
    assert out == RecordDocumentResult("v-1", "e-1", "2026/f.pdf")
    assert up.calls[0].request.url.path.endswith("uploadDocument")
    assert fl.calls[0].request.url.path.endswith("/visitDocument")
    b1 = json.loads(up.calls[0].request.content)
    assert b1 == {
        "content": base64.b64encode(b"%PDF-x").decode(),
        "format": "pdf",
        "patientUuid": "p-1",
        "encounterTypeName": "Patient Document",
        "fileType": "pdf",
    }
    b2 = json.loads(fl.calls[0].request.content)
    assert b2["patientUuid"] == "p-1"
    assert b2["visitTypeUuid"] == "vt-doc"
    assert b2["encounterTypeUuid"] == "et-doc"
    assert b2["providerUuid"] == "prov-1"
    assert b2["locationUuid"] == "loc-1"
    assert b2["encounterDateTime"] == AT_MS
    assert b2["documents"] == [{"testUuid": "c-lab", "image": "2026/f.pdf", "obsDateTime": AT_MS}]


@respx.mock
async def test_upload_image_file_type() -> None:
    _login()
    up = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "f.png"}))
    respx.post(FILE).mock(
        return_value=httpx.Response(200, json={"visitUuid": "v", "encounterUuid": "e"})
    )
    await _client().upload_document(_doc(mime="image/png"))
    b = json.loads(up.calls[0].request.content)
    assert (b["format"], b["fileType"]) == ("png", "image")


@respx.mock
async def test_upload_unknown_doc_type_raises_before_upload() -> None:
    _login()
    up = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "f"}))
    with pytest.raises(ConfigError):
        await _client().upload_document(_doc("nope"))
    assert up.call_count == 0


@respx.mock
async def test_upload_missing_config_raises_before_upload() -> None:
    _login()
    up = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "f"}))
    with pytest.raises(ConfigError):
        await _client(document_visit_type_uuid="").upload_document(_doc())
    assert up.call_count == 0


@respx.mock
async def test_upload_step1_failures_are_loud() -> None:
    _login()
    respx.post(UPLOAD).mock(return_value=httpx.Response(404))
    with pytest.raises(WriteNotSupported):
        await _client().upload_document(_doc())
    respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(VendorRejected):
        await _client().upload_document(_doc())


@respx.mock
@pytest.mark.parametrize(
    ("resp", "landed", "cause"),
    [
        (httpx.Response(404), False, "NotFound"),
        (httpx.Response(500), True, "TransientError"),
        (httpx.Response(400, json={"error": {"message": "bad"}}), False, "VendorRejected"),
        (httpx.Response(200, json={"visitUuid": "v"}), True, "MalformedResponse"),
    ],
)
async def test_step2_failures_carry_uploaded_url(
    resp: httpx.Response, landed: bool, cause: str
) -> None:
    _login()
    respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "2026/f.pdf"}))
    respx.post(FILE).mock(return_value=resp)
    with pytest.raises(DocumentFilingFailed) as ei:
        await _client().upload_document(_doc())
    assert ei.value.uploaded_url == "2026/f.pdf"
    assert ei.value.landed is landed
    assert ei.value.cause == cause


@respx.mock
async def test_file_uploaded_document_resumes_without_reupload() -> None:
    _login()
    up = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "x"}))
    fl = respx.post(FILE).mock(
        return_value=httpx.Response(200, json={"visitUuid": "v-1", "encounterUuid": "e-1"})
    )
    out = await _client().file_uploaded_document("p-1", "2026/f.pdf", "lab_report", "prov-1", AT)
    assert out == RecordDocumentResult("v-1", "e-1", "2026/f.pdf")
    assert up.call_count == 0
    assert json.loads(fl.calls[0].request.content)["documents"][0]["image"] == "2026/f.pdf"


@respx.mock
async def test_empty_ids_rejected_before_upload() -> None:
    _login()
    up = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "f"}))
    bad = RecordDocumentInput("", b"x", "application/pdf", "lab_report", "prov-1", AT)
    with pytest.raises(ValueError):
        await _client().upload_document(bad)
    bad2 = RecordDocumentInput("p", b"x", "application/pdf", "lab_report", "", AT)
    with pytest.raises(ValueError):
        await _client().upload_document(bad2)
    assert up.call_count == 0


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_step2_auth_failures_propagate_as_auth_error(status: int) -> None:
    _login()
    respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"url": "2026/f.pdf"}))
    respx.post(FILE).mock(return_value=httpx.Response(status))
    with pytest.raises(AuthError):
        await _client().upload_document(_doc())
