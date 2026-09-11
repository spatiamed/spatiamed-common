# HMS connectivity: per-integration TLS + vendor onboarding

Status: DESIGN — awaiting approval
Date: 2026-09-10

Closes the two gaps left after hosted-HMS testing.

## Gap 2 first: mostly already built

| Piece | State |
|---|---|
| Auth schemes (backend) | 5: api_key, bearer, hmac, oauth2_client_credentials, private_key_jwt |
| Field mapping | GenericRestAdapter is mapping-driven; LLM suggest-mapping endpoint exists |
| Portal UI | HmsConnectionCard / HmsAuthCard / HmsMappingCard + useHmsIntegration |
| Test Connection | live-proven against OpenEMR, hapi.fhir.org, r4.smarthealthit.org |

Two real holes, both small:

**2a. `private_key_jwt` missing from the portal.**
`hmsIntegrationForm.ts:43` lists four AUTH_SCHEMES. The backend supports five.
The missing one is SMART Backend Services — the scheme every hosted FHIR server
uses and the one the demo runbook itself uses. A hospital on SMART cannot be
configured through the portal at all today; it needs a DB write.
Fix: add the option + its fields (token_url, client_id, private_key_pem, kid, scopes).

**2b. No vendor-request email.**
The spec's "Connect your HMS" pre-filled email exists in neither repo.
This is the actual onboarding gap: it is how the hospital asks its own vendor
for base URL, auth scheme, scopes and format — and the hospital's request is
what authorises the integration.
Fix: a mailto/copy block in HmsConnectionCard, templated per selected vendor.

## Gap 1: per-integration TLS

All four adapters build the client identically, with no TLS configuration:

    bahmni.py:56        self._client = httpx.AsyncClient(timeout=30.0)
    fhir_r4.py:55       self._client = httpx.AsyncClient(timeout=30.0)
    mocdoc.py:45        self._client = httpx.AsyncClient(timeout=30.0)
    generic_rest.py:40  self._client = httpx.AsyncClient(timeout=30.0)

Three missing capabilities, one fix point.

1. **No client certificate** — mTLS is standard for backend PHI integrations.
2. **No per-integration CA trust.** The runbook's `SSL_CERT_FILE` workaround is
   *process-wide*: with multiple tenants on different on-prem HMSes, every
   hospital's CA lands in one shared bundle, so tenant A's CA becomes trusted
   for tenant B's connections. Poor isolation, and it does not scale.
3. **Hard-coded 30s timeout** — hosted servers returned in 15-20s.

### Shape

New `TlsConfig` read from `credentials_json` (already encrypted at rest):

    ca_bundle_pem     str | None   # extra CA to trust, in addition to system roots
    client_cert_pem   str | None   # mTLS client certificate chain
    client_key_pem    str | None   # mTLS private key
    timeout_seconds   float = 30.0

`build_tls_context(cfg) -> ssl.SSLContext | bool` in a new
`sm_common/integrations/tls.py`, passed to every adapter as `verify=`.

Verification is never disabled. There is no "insecure" switch — a hospital with
a private CA supplies `ca_bundle_pem`; that is the supported path.

### The disk constraint (measured, Python 3.14)

    load_verify_locations(cadata=<PEM string>)  -> works, in memory
    load_cert_chain(certfile=<PEM string>)      -> FileNotFoundError

CPython's ssl module has no in-memory client-key path. So:

- CA bundle: stays in memory, never touches disk.
- Client cert + key: written to a 0600 temp file, loaded into the SSLContext,
  and unlinked immediately. The key lives on disk for microseconds; the loaded
  context holds it in memory thereafter. Written under the process tmpdir.

### Change surface — 6 files

- new     sm_common/integrations/tls.py        build_tls_context + TlsConfig
- edit    sm_common/integrations/build_adapter.py  parse TLS keys out of credentials
- edit    4 adapters                           accept tls param, use it on the client

`auth.py`'s `_private_key_jwt_token` / `_oauth_token` take the *passed* client,
so the token endpoint inherits the same TLS posture for free. `health_check`
uses `self._client`, so Test Connection exercises the real TLS path — which is
what makes the portal button a genuine mTLS check.

Timeout: per-integration, default 30s, **clamped to <= 60s**. The poll worker
runs a 30s cycle; an unclamped tenant value would stack requests.

### Private key at rest

`credentials_json` already stores `private_key_pem` for `private_key_jwt` —
this decision was made when that scheme shipped. A client TLS key is the same
class of secret, so it goes in the same place, under the same FieldEncryptor.

One honest difference: a leaked SMART assertion key works only against the
token endpoint; a leaked client TLS key works against any endpoint that trusts
the certificate. Both impersonate us to the hospital. KMS-backed storage is the
upgrade path for both keys together, not for one of them alone.

### Portal surface

The TLS fields need a way in, or they are settable by nobody — the mirror
image of the "stored but never read" defect this codebase has shipped seven
times. A new `HmsTlsCard` carries `ca_bundle_pem`, `client_cert_pem`,
`client_key_pem` and `timeout_seconds`, alongside the existing auth card.

It has **no** verification-bypass control, and a test asserts the card renders
no "skip"/"disable"/"insecure" affordance, so one cannot be added quietly
later.

## Verification — the open decision

Nothing we can reach requires client certificates. OpenEMR does not, and
neither do hapi.fhir.org or r4.smarthealthit.org. So mTLS would ship
**unit-tested only** — exactly the mock-only posture that hid every defect
found this session (the `lt{until_date}` bound, the status allowlist that
ingested 0 of 137, three tests that could not fail).

Recommended: put a **Caddy** sidecar in front of the harness OpenEMR with
client-certificate verification required. Caddy 2.11-alpine is already the
platform's edge proxy (spatiamed-infra/caddy/Caddyfile); there is no nginx
anywhere in the platform, so nginx would mean a second reverse proxy to learn
and pin for no gain.

Validated against the pinned image (config adapted; only the absent cert file
errored, as expected):

    :8443 {
        tls /certs/server.crt /certs/server.key {
            client_auth {
                mode require_and_verify
                trust_pool file /certs/ca.crt
            }
        }
        reverse_proxy openemr:80
    }

Note `trust_pool file` — not the `trusted_ca_cert_file` form, which older
examples still show.

This gives a real server that *rejects* a connection presenting no client
certificate and *accepts* one presenting a trusted certificate, proving both
directions. Its self-signed CA also exercises `ca_bundle_pem` on the same
connection, so one sidecar verifies both halves of the TLS work.

Alternative: ship unit-tested and say so plainly in the runbook.

## Not in scope

- KMS/Vault migration for private keys (upgrade path, noted above)
- Certificate expiry monitoring (worth a follow-up issue)
- Per-vendor bespoke adapters beyond the existing four
