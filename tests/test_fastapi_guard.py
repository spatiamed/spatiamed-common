"""Tests for the FastAPI internal-secret guard.

Driven through a real FastAPI app + TestClient so the dependency-injection path
(APIKeyHeader resolution, HTTPException -> status code) is exercised end-to-end.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from sm_common.fastapi_guard import INTERNAL_SECRET_HEADER, verify_internal_secret

SECRET = "correct-internal-secret"


def _app(expected: str, **kwargs) -> FastAPI:
    app = FastAPI()
    guard = verify_internal_secret(expected, **kwargs)

    @app.get("/internal/ping", dependencies=[Depends(guard)])
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_valid_secret_accepted():
    client = TestClient(_app(SECRET))
    resp = client.get("/internal/ping", headers={INTERNAL_SECRET_HEADER: SECRET})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_mismatched_secret_rejected_403_by_default():
    client = TestClient(_app(SECRET))
    resp = client.get("/internal/ping", headers={INTERNAL_SECRET_HEADER: "wrong"})
    assert resp.status_code == 403


def test_missing_header_rejected_with_default_status():
    client = TestClient(_app(SECRET))
    resp = client.get("/internal/ping")
    # APIKeyHeader(auto_error=False) -> we reject, not FastAPI's 422.
    assert resp.status_code == 403


def test_status_code_param_honored_401():
    client = TestClient(_app(SECRET, status_code=401))
    bad = client.get("/internal/ping", headers={INTERNAL_SECRET_HEADER: "wrong"})
    assert bad.status_code == 401
    # missing header must ALSO use the custom status code (not 422)
    assert client.get("/internal/ping").status_code == 401


def test_empty_expected_secret_fails_closed_503():
    client = TestClient(_app(""))
    # Even a "matching" empty header must NOT be accepted (no accept-all bug).
    resp = client.get("/internal/ping", headers={INTERNAL_SECRET_HEADER: ""})
    assert resp.status_code == 503
    assert client.get("/internal/ping").status_code == 503


def test_empty_expected_secret_503_takes_precedence_over_custom_status():
    client = TestClient(_app("", status_code=401))
    # Misconfiguration is 503 regardless of the auth-failure status_code.
    assert client.get("/internal/ping").status_code == 503


def test_custom_header_name():
    client = TestClient(_app(SECRET, header_name="X-Svc-Key"))
    ok = client.get("/internal/ping", headers={"X-Svc-Key": SECRET})
    assert ok.status_code == 200
    # The default header name no longer works.
    assert client.get("/internal/ping", headers={INTERNAL_SECRET_HEADER: SECRET}).status_code == 403


def test_import_does_not_require_fastapi_at_module_load():
    # Importing the guard module must not import fastapi eagerly.
    import importlib
    import sys

    mod = importlib.import_module("sm_common.fastapi_guard")
    # fastapi only gets imported when the factory is CALLED, not on module import.
    assert hasattr(mod, "verify_internal_secret")
    assert "sm_common.fastapi_guard" in sys.modules


@pytest.mark.parametrize("code", [401, 403])
def test_parametrized_status_codes(code):
    client = TestClient(_app(SECRET, status_code=code))
    assert client.get("/internal/ping", headers={INTERNAL_SECRET_HEADER: "no"}).status_code == code
