#!/usr/bin/env python
"""Poll the OpenMRS session endpoint until Bahmni answers 200.

Env (required): BAHMNI_BASE_URL. Optional: BAHMNI_CA_PEM (path to a PEM bundle),
BAHMNI_INSECURE_TLS=1 (skip verification; a throwaway test box only).
Usage: python wait_ready.py [timeout_s]   exit 0 ready, 1 timed out, 2 bad env.
"""

from __future__ import annotations

import os
import ssl
import sys
import time
import urllib.error
import urllib.request

SESSION_PATH = "/openmrs/ws/rest/v1/session"


def tls_context() -> ssl.SSLContext:
    pem = os.environ.get("BAHMNI_CA_PEM")
    if pem:
        return ssl.create_default_context(cafile=pem)
    ctx = ssl.create_default_context()
    if os.environ.get("BAHMNI_INSECURE_TLS") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def wait_ready(base_url: str, timeout_s: int = 900, ctx: ssl.SSLContext | None = None) -> bool:
    """True once GET {base_url}/openmrs/ws/rest/v1/session returns 200, else False at timeout.

    Never raises: connection errors, 5xx and TLS failures all count as "not yet".
    """
    ctx = ctx or tls_context()
    url = base_url.rstrip("/") + SESSION_PATH
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5, context=ctx) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def main() -> int:
    base = os.environ.get("BAHMNI_BASE_URL")
    if not base:
        print("wait_ready: set BAHMNI_BASE_URL", file=sys.stderr)
        return 2
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    start = time.monotonic()
    ok = wait_ready(base, timeout)
    print(f"wait_ready: {'ready' if ok else 'TIMEOUT'} after {time.monotonic() - start:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
