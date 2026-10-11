"""Cloudflare Turnstile server-side verification (siteverify), fail-closed.

One shared verifier for every SpatiaMed public form (CareLoop consult intake,
web referral and camp registration; QueueCare public booking). It replaces the
per-service copies that only checked ``success`` and ignored ``hostname`` and
``action``, which let a token solved on any site using the same sitekey, or for
a different form, pass.

Usage::

    result = await verify_turnstile(
        token,
        secret=settings.turnstile_secret_key.get_secret_value(),
        expected_action="consult_intake",
        allowed_hostnames=settings.turnstile_hostnames,
        remoteip=trusted_client_ip,
    )
    if not result.ok:
        log.warning("captcha_rejected", reason=result.reason, error_codes=result.error_codes)
        raise HTTPException(422, "Invalid captcha")

Every failure mode rejects (``ok=False``):

* ``missing_secret``: the secret is unset or blank (an unconfigured deployment
  rejects everything rather than silently reopening the spam hole);
* ``missing_token``: the token is empty or blank;
* ``timeout`` / ``network_error``: siteverify could not be reached, after one
  retry that reuses the same ``idempotency_key``;
* ``http_status``: siteverify answered with a non-200 status;
* ``bad_json``: the body was not a JSON object;
* ``not_success``: Cloudflare said ``success != true`` (see ``error_codes``);
* ``action_mismatch``: the token was solved for a different widget action;
* ``hostname_not_allowed``: the token was solved on a hostname outside
  ``allowed_hostnames`` (an empty allowlist rejects everything).

* ``test_mode_forbidden``: ``test_mode=True`` reached a call whose
  ``environment`` is not a local/CI one (see below), so every token is rejected.

The token and the secret are never logged and never appear in the result.

Test mode (OD-3, owner 2026-10-09): local/CI E2E only
-----------------------------------------------------

Playwright cannot solve a real challenge, so the local/CI E2E stack runs with
``TURNSTILE_TEST_MODE=1``. Each service resolves the flag ONCE at startup::

    TEST_MODE = turnstile_test_mode(environment=settings.ENVIRONMENT)  # raises in staging/prod

and passes it per call::

    await verify_turnstile(token, ..., test_mode=TEST_MODE, environment=settings.ENVIRONMENT)

With test mode on, exactly :data:`TURNSTILE_TEST_TOKEN` (Cloudflare's own dummy
token, which its always-pass test sitekeys emit) passes without calling
Cloudflare, reporting ``action=expected_action`` and the first allowed hostname
(an empty allowlist still rejects).
Every other token still goes to siteverify. The secret is still required.
Test mode is refused unless ``environment`` is one of
:data:`TURNSTILE_TEST_MODE_ENVIRONMENTS`: :func:`turnstile_test_mode` raises
:class:`TurnstileTestModeForbiddenError`, and :func:`verify_turnstile` rejects
every token with ``test_mode_forbidden``. That covers staging, prod/production,
and an unset or unknown environment (fail closed).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx

__all__ = [
    "TURNSTILE_TEST_MODE_ENVIRONMENTS",
    "TURNSTILE_TEST_MODE_ENV_VAR",
    "TURNSTILE_TEST_TOKEN",
    "TURNSTILE_VERIFY_URL",
    "TurnstileResult",
    "TurnstileTestModeForbiddenError",
    "turnstile_test_mode",
    "verify_turnstile",
]

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Cloudflare documents a 2048-character maximum token length.
_MAX_TOKEN_LENGTH = 2048

#: The one token test mode accepts: Cloudflare's dummy token, emitted by its
#: always-pass test sitekeys (e.g. ``1x00000000000000000000AA``).
TURNSTILE_TEST_TOKEN: Final[str] = "XXXX.DUMMY.TOKEN.XXXX"

#: Env var that switches test mode on (``1`` / ``true`` / ``yes`` / ``on``).
TURNSTILE_TEST_MODE_ENV_VAR: Final[str] = "TURNSTILE_TEST_MODE"

#: The ONLY environments where test mode may run (compared trimmed and
#: case-insensitively). Anything else, including staging, prod, production and
#: an empty/unknown value, is refused.
TURNSTILE_TEST_MODE_ENVIRONMENTS: Final[frozenset[str]] = frozenset(
    {"dev", "development", "local", "test", "ci"}
)

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


class TurnstileTestModeForbiddenError(RuntimeError):
    """Test mode was enabled outside a local/CI environment (e.g. staging/prod)."""


def _test_mode_environment_allowed(environment: str | None) -> bool:
    return (environment or "").strip().lower() in TURNSTILE_TEST_MODE_ENVIRONMENTS


def turnstile_test_mode(*, environment: str, enabled: bool | None = None) -> bool:
    """Resolve whether Turnstile test mode is on. Call once at service startup.

    Args:
        environment: The service's ``ENVIRONMENT`` setting (pass the validated
            settings value, not ``os.environ``, which may be empty when the
            settings came from a ``.env`` file).
        enabled: Explicit switch. When ``None``, read
            :data:`TURNSTILE_TEST_MODE_ENV_VAR` (``1``/``true``/``yes``/``on``).

    Returns:
        True when test mode is on (and allowed), False when it is off.

    Raises:
        TurnstileTestModeForbiddenError: test mode is on and ``environment`` is
            not in :data:`TURNSTILE_TEST_MODE_ENVIRONMENTS` (staging, prod,
            production, empty or unknown). Let it crash the startup.
    """
    if enabled is None:
        raw = os.environ.get(TURNSTILE_TEST_MODE_ENV_VAR, "")
        enabled = raw.strip().lower() in _TRUTHY
    if not enabled:
        return False
    if not _test_mode_environment_allowed(environment):
        raise TurnstileTestModeForbiddenError(
            f"{TURNSTILE_TEST_MODE_ENV_VAR} is on but ENVIRONMENT={environment!r} is not one "
            f"of {sorted(TURNSTILE_TEST_MODE_ENVIRONMENTS)}; refusing (local/CI E2E only)"
        )
    return True


@dataclass(frozen=True, slots=True)
class TurnstileResult:
    """Outcome of one siteverify check. Carries no token and no secret.

    Attributes:
        ok: True only when every check passed.
        reason: ``"ok"`` or the first failing check (see the module docstring).
        error_codes: Cloudflare's ``error-codes`` when it returned any.
        hostname: The hostname Cloudflare reported for the solve, if any.
        action: The widget action Cloudflare reported for the solve, if any.
    """

    ok: bool
    reason: str
    error_codes: tuple[str, ...] = ()
    hostname: str | None = None
    action: str | None = None


def _reject(
    reason: str,
    *,
    error_codes: tuple[str, ...] = (),
    hostname: str | None = None,
    action: str | None = None,
) -> TurnstileResult:
    return TurnstileResult(
        ok=False, reason=reason, error_codes=error_codes, hostname=hostname, action=action
    )


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def _post_with_one_retry(
    client: httpx.AsyncClient, form: dict[str, str], timeout: float
) -> httpx.Response:
    """POST the form; on a transport error retry exactly once with the same form.

    The form carries the ``idempotency_key``, so Cloudflare treats the retry as
    the same verification rather than a second redemption of the token.
    """
    try:
        return await client.post(TURNSTILE_VERIFY_URL, data=form, timeout=timeout)
    except httpx.TransportError:
        return await client.post(TURNSTILE_VERIFY_URL, data=form, timeout=timeout)


async def verify_turnstile(
    token: str,
    *,
    secret: str,
    expected_action: str,
    allowed_hostnames: frozenset[str],
    remoteip: str | None = None,
    idempotency_key: str | None = None,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
    test_mode: bool = False,
    environment: str | None = None,
) -> TurnstileResult:
    """Verify a Turnstile token with Cloudflare siteverify. Fails closed.

    Args:
        token: The ``cf-turnstile-response`` value the browser submitted.
        secret: The widget's secret key. Blank rejects every token.
        expected_action: The ``action`` the widget was rendered with; the solve
            must report exactly this action.
        allowed_hostnames: Hostnames the solve may come from (compared
            case-insensitively). An empty set rejects every token.
        remoteip: The trusted client IP (from the proxy chain, never a
            client-controlled leftmost X-Forwarded-For hop). Optional.
        idempotency_key: Sent to Cloudflare and reused on the single retry.
            A uuid4 is generated when omitted.
        timeout: Per-attempt timeout in seconds.
        client: An ``httpx.AsyncClient`` to reuse (tests inject one). When
            omitted, a client is created and closed for this call.
        test_mode: Local/CI E2E only (resolve it with
            :func:`turnstile_test_mode` at startup). Accepts exactly
            :data:`TURNSTILE_TEST_TOKEN` without calling Cloudflare.
        environment: The service's ``ENVIRONMENT``. Required with
            ``test_mode=True``: outside :data:`TURNSTILE_TEST_MODE_ENVIRONMENTS`
            (or when omitted) every token is rejected with
            ``test_mode_forbidden``.
    """
    if not secret or not secret.strip():
        return _reject("missing_secret")
    if not token or not token.strip() or len(token) > _MAX_TOKEN_LENGTH:
        return _reject("missing_token")

    if test_mode:
        if not _test_mode_environment_allowed(environment):
            return _reject("test_mode_forbidden")
        if token == TURNSTILE_TEST_TOKEN:
            allowed_sorted = sorted(h.lower() for h in allowed_hostnames)
            if not allowed_sorted:  # an empty allowlist rejects everything, here too
                return _reject("hostname_not_allowed", action=expected_action)
            return TurnstileResult(
                ok=True, reason="ok", hostname=allowed_sorted[0], action=expected_action
            )

    form: dict[str, str] = {
        "secret": secret,
        "response": token,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
    if remoteip:
        form["remoteip"] = remoteip

    try:
        if client is not None:
            response = await _post_with_one_retry(client, form, timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as own_client:
                response = await _post_with_one_retry(own_client, form, timeout)
    except httpx.TimeoutException:
        return _reject("timeout")
    except httpx.HTTPError:
        return _reject("network_error")

    if response.status_code != 200:
        return _reject("http_status")

    try:
        data = response.json()
    except ValueError:
        return _reject("bad_json")
    if not isinstance(data, dict):
        return _reject("bad_json")

    raw_codes = data.get("error-codes")
    error_codes = (
        tuple(c for c in raw_codes if isinstance(c, str)) if isinstance(raw_codes, list) else ()
    )
    hostname = _str_or_none(data.get("hostname"))
    action = _str_or_none(data.get("action"))

    if data.get("success") is not True:
        return _reject("not_success", error_codes=error_codes, hostname=hostname, action=action)
    if action != expected_action:
        return _reject("action_mismatch", error_codes=error_codes, hostname=hostname, action=action)
    allowed = {h.lower() for h in allowed_hostnames}
    if hostname is None or hostname.lower() not in allowed:
        return _reject(
            "hostname_not_allowed", error_codes=error_codes, hostname=hostname, action=action
        )

    return TurnstileResult(
        ok=True, reason="ok", error_codes=error_codes, hostname=hostname, action=action
    )
