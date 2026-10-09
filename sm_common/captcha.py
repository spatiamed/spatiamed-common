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

The token and the secret are never logged and never appear in the result.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "TURNSTILE_VERIFY_URL",
    "TurnstileResult",
    "verify_turnstile",
]

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Cloudflare documents a 2048-character maximum token length.
_MAX_TOKEN_LENGTH = 2048


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
    """
    if not secret or not secret.strip():
        return _reject("missing_secret")
    if not token or not token.strip() or len(token) > _MAX_TOKEN_LENGTH:
        return _reject("missing_token")

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
