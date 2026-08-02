"""FastAPI dependency for the ``X-Internal-Secret`` service-to-service contract.

This is the SERVER side of the contract implemented by
``sm_common.internal_http.InternalClient``. It is kept in a separate module (and
imports ``fastapi`` lazily, inside the factory) so that ``import sm_common`` and
the rest of the core library never require FastAPI to be installed. FastAPI is an
optional extra::

    pip install "spatiamed-common[fastapi] @ git+https://github.com/spatiamed/spatiamed-common.git@vX"

Usage::

    from sm_common.fastapi_guard import verify_internal_secret

    require_internal = verify_internal_secret(settings.internal_secret)

    @router.post("/internal/foo", dependencies=[Depends(require_internal)])
    async def foo() -> ...:
        ...

Consumers diverge on the failure status code and the config var NAME that holds
the secret, so the factory is parameterizable to preserve each caller's contract:

    * platform-api uses 403 on mismatch (the default here).
    * CareLoop uses 401 -> pass ``status_code=401``.
    * A custom header name -> pass ``header_name=...``.

Fail-closed behaviour (never accept-all):
    * empty ``expected_secret`` (service misconfigured) -> 503, always rejected.
      (This deliberately does NOT replicate CareLoop's latent bug where two empty
      strings compared equal and let traffic through.)
    * missing header (``APIKeyHeader(auto_error=False)`` -> None) -> ``status_code``.
    * mismatched header -> ``status_code``.
    * constant-time comparison over bytes (``hmac.compare_digest``).
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

INTERNAL_SECRET_HEADER = "X-Internal-Secret"


def verify_internal_secret(
    expected_secret: str,
    *,
    status_code: int = 403,
    header_name: str = INTERNAL_SECRET_HEADER,
) -> Callable[..., None]:
    """Build a FastAPI dependency that enforces the internal-secret header.

    Args:
        expected_secret: The configured shared secret. If empty/falsy, the
            dependency fail-closes with 503 (the service is misconfigured and
            must not accept internal traffic).
        status_code: Status raised when the header is missing or mismatched.
            Default 403; pass 401 to preserve a consumer that currently 401s.
        header_name: Name of the header carrying the secret. Default
            ``X-Internal-Secret``.

    Returns:
        A dependency callable suitable for ``Depends(...)`` that returns ``None``
        on success and raises ``fastapi.HTTPException`` otherwise.
    """
    # Lazy imports so the core library never hard-depends on fastapi.
    from fastapi import Depends, HTTPException  # noqa: PLC0415
    from fastapi.security import APIKeyHeader  # noqa: PLC0415

    header_scheme = APIKeyHeader(name=header_name, auto_error=False)

    def _dependency(provided: str | None = Depends(header_scheme)) -> None:
        if not expected_secret:
            raise HTTPException(status_code=503, detail="internal_secret_not_configured")
        if provided is None or not hmac.compare_digest(provided.encode(), expected_secret.encode()):
            raise HTTPException(status_code=status_code, detail="invalid_internal_secret")

    return _dependency
