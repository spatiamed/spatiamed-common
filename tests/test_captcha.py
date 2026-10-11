"""sm_common.captcha.verify_turnstile: every reject path, the happy path, the retry.

siteverify is never called for real: each test injects an ``httpx.AsyncClient``
over a ``MockTransport`` (or uses respx for the self-owned-client path).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from sm_common.captcha import TURNSTILE_VERIFY_URL, TurnstileResult, verify_turnstile

SECRET = "unit-test-secret"
TOKEN = "unit-test-token-abc"
ACTION = "consult_intake"
HOSTS = frozenset({"visit.staging.spatiamed.com"})

GOOD_BODY = {
    "success": True,
    "hostname": "visit.staging.spatiamed.com",
    "action": ACTION,
    "error-codes": [],
}


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(body: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


async def _verify(
    handler: Callable[[httpx.Request], httpx.Response], **overrides: object
) -> TurnstileResult:
    kwargs: dict[str, object] = {
        "secret": SECRET,
        "expected_action": ACTION,
        "allowed_hostnames": HOSTS,
    }
    kwargs.update(overrides)
    token = kwargs.pop("token", TOKEN)
    async with _client(handler) as client:
        return await verify_turnstile(token, client=client, **kwargs)  # type: ignore[arg-type]


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("siteverify must not be called")


async def test_happy_path_sends_form_and_returns_ok() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=GOOD_BODY)

    result = await _verify(handler, remoteip="203.0.113.7", idempotency_key="key-1")

    assert result == TurnstileResult(
        ok=True,
        reason="ok",
        error_codes=(),
        hostname="visit.staging.spatiamed.com",
        action=ACTION,
    )
    assert len(seen) == 1
    assert str(seen[0].url) == TURNSTILE_VERIFY_URL
    assert _form(seen[0]) == {
        "secret": SECRET,
        "response": TOKEN,
        "remoteip": "203.0.113.7",
        "idempotency_key": "key-1",
    }


async def test_hostname_match_is_case_insensitive() -> None:
    body = {**GOOD_BODY, "hostname": "Visit.Staging.SpatiaMed.com"}
    assert (await _verify(_json(body))).ok is True


async def test_token_and_secret_never_in_result() -> None:
    for handler in (_json(GOOD_BODY), _json({"success": False}), _json({}, 500)):
        result = await _verify(handler)
        assert TOKEN not in repr(result)
        assert SECRET not in repr(result)


@pytest.mark.parametrize("secret", ["", "   "])
async def test_missing_secret_rejects_without_calling(secret: str) -> None:
    result = await _verify(_never_called, secret=secret)
    assert (result.ok, result.reason) == (False, "missing_secret")


@pytest.mark.parametrize("token", ["", "  ", "x" * 2049])
async def test_missing_token_rejects_without_calling(token: str) -> None:
    result = await _verify(_never_called, token=token)
    assert (result.ok, result.reason) == (False, "missing_token")


@pytest.mark.parametrize("status", [400, 429, 500, 503])
async def test_non_200_rejects(status: int) -> None:
    result = await _verify(_json(GOOD_BODY, status))
    assert (result.ok, result.reason) == (False, "http_status")


async def test_timeout_rejects_after_one_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    result = await _verify(handler)
    assert (result.ok, result.reason) == (False, "timeout")
    assert calls == 2


async def test_network_error_rejects_after_one_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down", request=request)

    result = await _verify(handler)
    assert (result.ok, result.reason) == (False, "network_error")
    assert calls == 2


@pytest.mark.parametrize("key", ["caller-key", None])
async def test_retry_reuses_the_same_idempotency_key(key: str | None) -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(_form(request)["idempotency_key"])
        if len(keys) == 1:
            raise httpx.ConnectError("blip", request=request)
        return httpx.Response(200, json=GOOD_BODY)

    result = await _verify(handler, idempotency_key=key)

    assert result.ok is True
    assert len(keys) == 2
    assert keys[0] == keys[1]
    if key is None:
        assert uuid.UUID(keys[0]).version == 4
    else:
        assert keys[0] == key


async def test_non_200_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    await _verify(handler)
    assert calls == 1


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b'"success"'])
async def test_bad_json_rejects(content: bytes) -> None:
    result = await _verify(lambda request: httpx.Response(200, content=content))
    assert (result.ok, result.reason) == (False, "bad_json")


@pytest.mark.parametrize("success", [False, None, "true", 1])
async def test_success_not_true_rejects(success: object) -> None:
    body = {**GOOD_BODY, "success": success, "error-codes": ["invalid-input-response"]}
    result = await _verify(_json(body))
    assert (result.ok, result.reason) == (False, "not_success")
    assert result.error_codes == ("invalid-input-response",)


async def test_success_missing_rejects() -> None:
    body = {k: v for k, v in GOOD_BODY.items() if k != "success"}
    result = await _verify(_json(body))
    assert (result.ok, result.reason) == (False, "not_success")


@pytest.mark.parametrize("action", ["web_referral", "", None])
async def test_action_mismatch_rejects(action: str | None) -> None:
    body = {**GOOD_BODY, "action": action}
    result = await _verify(_json(body))
    assert (result.ok, result.reason) == (False, "action_mismatch")
    assert result.action == action


@pytest.mark.parametrize("hostname", ["evil.example.com", "", None])
async def test_hostname_not_allowed_rejects(hostname: str | None) -> None:
    body = {**GOOD_BODY, "hostname": hostname}
    result = await _verify(_json(body))
    assert (result.ok, result.reason) == (False, "hostname_not_allowed")
    assert result.hostname == hostname


async def test_empty_allowlist_rejects_everything() -> None:
    result = await _verify(_json(GOOD_BODY), allowed_hostnames=frozenset())
    assert (result.ok, result.reason) == (False, "hostname_not_allowed")


@respx.mock
async def test_self_owned_client_path() -> None:
    route = respx.post(TURNSTILE_VERIFY_URL).mock(
        return_value=httpx.Response(200, content=json.dumps(GOOD_BODY).encode())
    )
    result = await verify_turnstile(
        TOKEN, secret=SECRET, expected_action=ACTION, allowed_hostnames=HOSTS
    )
    assert result.ok is True
    assert route.call_count == 1
    assert "remoteip" not in _form(route.calls[0].request)


# --- OD-3: test mode for the local/CI E2E stack only --------------------------

BAD_TOKEN_BODY = {"success": False, "error-codes": ["invalid-input-response"]}

from sm_common.captcha import (  # noqa: E402
    TURNSTILE_TEST_TOKEN,
    TurnstileTestModeForbiddenError,
    turnstile_test_mode,
)


def test_test_token_is_cloudflares_dummy_token() -> None:
    # Cloudflare's always-pass test sitekeys emit exactly this token, so the
    # E2E stack can render the real widget with a test sitekey.
    assert TURNSTILE_TEST_TOKEN == "XXXX.DUMMY.TOKEN.XXXX"


async def test_test_mode_accepts_the_fixed_token_without_calling_cloudflare() -> None:
    result = await _verify(
        _never_called, token=TURNSTILE_TEST_TOKEN, test_mode=True, environment="ci"
    )
    assert result == TurnstileResult(
        ok=True, reason="ok", error_codes=(), hostname="visit.staging.spatiamed.com", action=ACTION
    )


async def test_test_mode_reports_the_first_allowed_hostname_sorted() -> None:
    result = await _verify(
        _never_called,
        token=TURNSTILE_TEST_TOKEN,
        test_mode=True,
        environment="dev",
        allowed_hostnames=frozenset({"b.example", "a.example"}),
    )
    assert result.ok and result.hostname == "a.example"


async def test_test_mode_off_sends_the_fixed_token_to_cloudflare() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=BAD_TOKEN_BODY)

    result = await _verify(handler, token=TURNSTILE_TEST_TOKEN)
    assert len(seen) == 1
    assert not result.ok and result.reason == "not_success"


async def test_test_mode_sends_any_other_token_to_cloudflare() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=BAD_TOKEN_BODY)

    result = await _verify(handler, token="some-other-token", test_mode=True, environment="dev")
    assert len(seen) == 1 and _form(seen[0])["response"] == "some-other-token"
    assert not result.ok and result.reason == "not_success"


async def test_test_mode_still_requires_a_secret() -> None:
    result = await _verify(
        _never_called, token=TURNSTILE_TEST_TOKEN, test_mode=True, environment="dev", secret=""
    )
    assert not result.ok and result.reason == "missing_secret"


@pytest.mark.parametrize("environment", ["dev", "development", "local", "test", "ci", " DEV "])
def test_test_mode_allowed_on_local_and_ci(environment: str) -> None:
    assert turnstile_test_mode(environment=environment, enabled=True) is True


@pytest.mark.parametrize(
    "environment", ["staging", "prod", "production", "PROD", "Staging", "", "  ", "qa", "unknown"]
)
def test_test_mode_refused_outside_local_and_ci(environment: str) -> None:
    with pytest.raises(TurnstileTestModeForbiddenError):
        turnstile_test_mode(environment=environment, enabled=True)


@pytest.mark.parametrize("environment", ["staging", "prod", "dev", ""])
def test_test_mode_disabled_never_raises(environment: str) -> None:
    assert turnstile_test_mode(environment=environment, enabled=False) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("", False),
        ("no", False),
    ],
)
def test_test_mode_reads_the_env_var(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("TURNSTILE_TEST_MODE", value)
    assert turnstile_test_mode(environment="dev") is expected


def test_test_mode_env_var_unset_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TURNSTILE_TEST_MODE", raising=False)
    assert turnstile_test_mode(environment="prod") is False


def test_test_mode_env_var_on_in_prod_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURNSTILE_TEST_MODE", "1")
    with pytest.raises(TurnstileTestModeForbiddenError):
        turnstile_test_mode(environment="prod")


@pytest.mark.parametrize("environment", ["staging", "prod", "production", None])
async def test_verify_fails_closed_when_test_mode_reaches_a_forbidden_environment(
    environment: str | None,
) -> None:
    # Belt and braces: test mode is re-checked per call, and an environment
    # that is forbidden OR not passed at all rejects every token (fail closed),
    # never the test bypass.
    result = await _verify(
        _never_called, token=TURNSTILE_TEST_TOKEN, test_mode=True, environment=environment
    )
    assert not result.ok and result.reason == "test_mode_forbidden"
