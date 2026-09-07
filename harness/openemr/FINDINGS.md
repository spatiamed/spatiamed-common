# Can OpenEMR prove bidirectional real-time HMS sync?

Investigation date: 2026-09-07. Target: OpenEMR 8.3.0 (`v8_3_0`, released 2026-08-18).

**Status: run live against OpenEMR 8.3.0 on 2026-09-07.** §7 records what was
measured against the running server; §2-§4 mix measurement with source reading
and each claim says which.

## Short answer

OpenEMR is a good test HMS for the **adapter** layer, and it is the right tool for
that job. It cannot yet test what was actually asked — a kiosk/AI-agent booking
round-tripping with an HMS — because **neither direction of that round trip is
connected to anything today.** Both ends terminate before they reach data.

## 1. The blocker (found in our code, not OpenEMR's)

**Inbound — fetched, then discarded.** `hms_poll_worker.py` polls every 30s,
gets appointments back from the adapter, and then:

> `# NOTE: appointment -> STAFF_TASK booking persistence is deferred. ... For now
> we log receipt and advance the cursor.`

The appointment loop body is a single `logger.debug`. The cursor advances, health
flips to `ok`, `records_fetched` is recorded, and the appointments are dropped on
the floor. It then logs `"Synced %d appointments"` at INFO — which reads as
success in the logs while nothing was persisted.

**Outbound — never invoked.** `WriteBackSaga` is registered on the Temporal
worker (`server/app/temporal/worker.py:43`), but nothing starts it. The only
`start_workflow` calls anywhere in `server/app/` are telemedicine webhooks,
prescription templates, and transcript ingest. No kiosk path and no voice-agent
path reaches HMS write-back.

So the honest status is: adapters and transport exist and are tested against
mocks; the first and last mile of the sync do not exist.

## 2. OpenEMR's API surface vs. our `HmsAdapter` contract

Derived from `apis/routes/_rest_routes_{standard,fhir_r4_us_core_3_1_0}.inc.php`
at tag `v8_3_0`.

| `HmsAdapter` method | What `FhirR4Adapter` sends | OpenEMR FHIR | Verdict |
|---|---|---|---|
| `health_check` | `GET /metadata` | present | works |
| `find_patient` | `GET /Patient?identifier=` | `GET /fhir/Patient` | works |
| `list_appointments_modified_since` | `GET /Appointment?_lastUpdated=…` | `GET /fhir/Appointment`, `_lastUpdated` supported (maps to `pc_time`) | works — but see bug 4.1 |
| `fetch_doctor_roster` | `GET /Practitioner` | `GET /fhir/Practitioner` | works |
| `fetch_recent_bookings` | `GET /Appointment` | as above | works |
| `write_back_idempotent` | `PUT /Appointment/{id}` | **no POST/PUT route on Appointment** | fails (404) |
| `cancel` | `PUT /Appointment/{id}` | **no POST/PUT route on Appointment** | fails (404) |
| `push_visit_event` | `POST /Encounter` | **FHIR Encounter is GET-only** | fails (404) |

All five read methods map cleanly. **All three write methods fail** — OpenEMR's
FHIR surface is read-only for Appointment and Encounter.

The writes do exist on OpenEMR's **Standard (non-FHIR) API**:

- `POST /api/patient/:pid/appointment` — create
- `DELETE /api/patient/:pid/appointment/:eid` — cancel
- `POST|PUT /api/patient/:puuid/encounter` — visit lifecycle

There is no `PUT`/`PATCH` on appointment anywhere, so appointment **status**
updates (arrived / in-progress / finished) have no target in either API.

## 3. Two constraints that shape any harness

**No push, anywhere.** OpenEMR exposes no FHIR `Subscription` resource. There is
no webhook or subscription intake on our side either. "Real time" therefore has a
hard floor of the 30s poll inbound (`POLL_INTERVAL = 30`); outbound is seconds.
Worth stating plainly to anyone who hears "real-time sync".

**The Standard API refuses system tokens.** The routes file states it directly:
*"the api route is only for users role."* System-scoped `client_credentials`
tokens can reach every read endpoint and no write endpoint, so the outbound leg
needs a separate user-role token. OpenEMR also requires RS384 `private_key_jwt`
(client assertion + registered `jwks_uri`) for system scopes — see bug 4.3.

## 4. Bugs found before OpenEMR even started

### 4.1 The FHIR poll can never see a same-day change — HIGH

`FhirR4Adapter.list_appointments_modified_since` sends:

```python
"_lastUpdated": [f"gt{since}", f"lt{until_date.isoformat()}"]
```

`until_date` is the tenant's **clinic day** (today) from `clinic_day()`. Applying
it as an upper bound on `_lastUpdated` — the *modification* timestamp — with a
date-only value means `lt2026-09-07`, i.e. *strictly before midnight this
morning*. Every appointment booked, changed, or cancelled today is excluded.

The 30-second poll cadence exists to catch same-day churn, and this filter
removes exactly that. `until_date` looks intended as an appointment-**date**
horizon (the `date` search param), not a modification-time ceiling.

The other three adapters (`bahmni`, `mocdoc`, `generic_rest`) ignore `until_date`
entirely, so only the FHIR path carries this.

**Why mocks missed it:** `test_until_date_sent_as_lt_upper_bound` asserts the
request contains `lt2026-06-30` and calls it "finding #1". The test locks the
behaviour in. respx replays a canned bundle regardless of the query, so no test
can observe that the filter excludes today's data.

### 4.2 Outbound write-back is not idempotent against a real vendor — HIGH

`GenericRestAdapter.write_back_idempotent` POSTs the booking with an
`idempotencyKey` field in the body and treats HTTP 409 as a conflict. That works
only if the vendor implements that field. OpenEMR — like most vendors — ignores
unknown fields and creates a row. There is no search-before-create and no
natural-key lookup, so **every saga retry creates a duplicate appointment.**

`FhirR4Adapter` has the mirror problem: it relies on `PUT /Appointment/{our-uuid}`
(FHIR update-as-create with a client-assigned id) plus a non-standard
`X-Idempotency-Key` header that no FHIR server honors.

### 4.3 `auth.py` cannot do SMART Backend Services — MEDIUM

`build_auth_headers`' `oauth2_client_credentials` scheme form-posts
`client_id` + `client_secret`. OpenEMR requires an RS384-signed
`private_key_jwt` client assertion against a registered `jwks_uri`. This is the
SMART Backend Services standard, so it will recur with FHIR/ABDM vendors, not
just OpenEMR. There is no `private_key_jwt` scheme in `auth.py` today.

### 4.4 FHIR write failures never fall through to the next tier — MEDIUM

`WriteBackRouter` falls through on a raised `TransientError` and stops on a raised
`ConflictError`. `GenericRestAdapter` raises. `FhirR4Adapter` **returns**
`WriteBackResult(status="TRANSIENT_ERROR")` instead. The router treats that as a
terminal outcome and returns `status="TRANSIENT_ERROR"` — a value outside its own
documented set (`SUCCESS | CONFLICT | MANUAL_REQUIRED | NO_ADAPTERS`). A FHIR
vendor outage therefore never escalates to the `agent` or `manual` tier.

## 5. What a harness can prove, and at which layer

- **L1 — adapter conformance.** Run the production adapters against a live
  OpenEMR: auth, the five reads, and the write attempts. Proves 4.1–4.4 and
  catches the whole class of bug respx cannot. Cheap, and it is the prerequisite.
- **L2 — the round trip actually asked about.** Kiosk/agent booking → OpenEMR,
  and OpenEMR appointment → QueueCare queue. Needs QueueCare's stack alongside,
  plus the two missing miles from §1 built first.

L1 is buildable now. L2 is blocked on §1 — and §1 is a feature gap, not a test
gap.

## 6. Notes for whoever runs this

- A local `mysqld` already owns port 3306 on this machine. The harness DB port is
  deliberately unpublished; publishing it invites the wrong database to answer.
- OpenEMR's first boot runs its installer and takes several minutes.
- A freshly registered OAuth client is inert until enabled; the harness scripts
  this rather than requiring a human in the admin UI.

## 7. Live run — measured

Stack: `docker compose up` here. First boot ~4 min (installer). Auth via
`setup_client.py` (system reads) and `seed_data.py` (user-role writes). Probes:
`probe_conformance.py`, `probe_gaps.py`.

```
READS (FhirR4Adapter inbound):
  [PASS   ] health_check: GET /metadata -> 200
  [PASS   ] find_patient: GET /Patient?identifier= -> 200
  [PASS   ] fetch_doctor_roster: GET /Practitioner -> 200
  [PASS   ] list_appointments (adapter query): -> 200, 0 entries (lt2026-09-07)
  [INFO   ] list_appointments (no lt bound): -> 200, 1 entries

WRITES (FhirR4Adapter outbound):
  [FAIL   ] write_back_idempotent: PUT /Appointment/{id} -> 404
  [FAIL   ] cancel: same route as write_back_idempotent
  [FAIL   ] push_visit_event: POST /Encounter -> 404
```

### 7.1 Bug 4.1 confirmed — measured

One appointment created via the Standard API, dated today. The production
adapter's own query returned **0 entries**; the identical query without the
`lt{until_date}` upper bound returned **1**. A same-day appointment is invisible
to the poller against a real FHIR server.

This is the measurement respx cannot produce: the mock replays a canned bundle
whatever the query says, which is why `test_until_date_sent_as_lt_upper_bound`
passes while asserting the broken shape.

### 7.2 A failed create returns HTTP 200 — measured

```
POST /api/patient/2/appointment   (pc_hometext omitted)
HTTP 200  {"pc_hometext":{"Required::NON_EXISTENT_KEY":"pc_hometext must be provided, but does not exist"}}
```

Nothing is created. `GenericRestAdapter.write_back_idempotent` calls
`raise_for_status()` (passes on 200) then reads the id field out of the body,
producing `WriteBackResult(status="SUCCESS", hms_booking_id=None)` — a rejected
booking recorded as written. Adapters must validate the body, not the status
line.

This one bit the harness itself: a batch of seed appointments reported success
and created nothing, which is exactly how it would fail in production.

### 7.3 Result sets truncate silently — measured

Corrected from an earlier guess that `_count` was ignored.

| Query | entries | `total` | `next` link |
|---|---|---|---|
| no `_count` (17 rows in DB) | 17 | 17 | absent |
| `_count=5` | 5 | **5** | absent |

`_count` **is** honoured, but `total` reports the page size rather than the
match count, and OpenEMR never emits a `link[relation=next]`. The adapter sends
`_count=100` and loops on `_next_url`, so with more than 100 modified
appointments it ingests the first 100, sees `total=100`, finds no next link, and
concludes it has everything. Silent data loss with no signal — worse than an
error.

### 7.4 Standard API refuses system tokens — measured

```
POST /api/patient/2/appointment  with the system-scoped token
HTTP 403  "API call failed due to insufficient permissions for the requested resource."
```

Confirms the routes file's *"the api route is only for users role"*. The harness
therefore holds **two** tokens: a system `client_credentials` token for FHIR
reads and a user-role password-grant token for Standard-API writes. That has a
design consequence — see §8.

### 7.5 The adapter puts a raw phone number in `phone_hash` — measured, HIGH

OpenEMR returned, for the seeded patient:

```json
"telecom":    [{"system": "phone", "value": "9000000001", "use": "mobile"}]
"identifier": [{"value": "1", ...}]
```

`FhirR4Adapter._patient_to_canonical` does:

```python
if telecom.get("system") == "phone":
    phone_hash = telecom.get("value", "")
```

It assigns the **unhashed phone number** to a field every downstream consumer
treats as a salted hash — the adapter carries a `_hash_salt` and never applies it
here. Two consequences: patient matching against stored hashes can never hit, and
a raw phone number flows into logs and storage under a name asserting it is
hashed.

Also visible above: OpenEMR's `identifier[].value` is the internal `pid` (`"1"`),
not a hospital MRN — `pubpid` is not emitted. Any MRN-based matching has to treat
that as vendor-specific.

### 7.6 Discovery under-reports the server — measured

`grant_types_supported` lists only `authorization_code`, `password`,
`refresh_token`; `token_endpoint_auth_methods_supported` only
`client_secret_post`. Yet `client_credentials` with an RS384 `private_key_jwt`
assertion works — it authenticated every read above. Capability detection must
probe, not trust discovery.

### 7.7 Inbound latency floor is entirely ours — measured

Create → visible to the corrected poller query: **0.09s**. `meta.lastUpdated` is
second-granular. The HMS is effectively instant; the whole inbound delay is our
`POLL_INTERVAL = 30`. If sync needs to be faster, the poll interval is the only
lever — there is no push to subscribe to.

### 7.8 Outbound failures are swallowed — inferred from source, exercised here

`push_visit_event` logs a warning on any non-2xx and returns `None`; the caller
cannot tell the event never landed. Against OpenEMR that is a 404 on **every**
call, silently. `cancel` similarly returns `CancelResult(status="FAILED")` rather
than raising, so the saga's compensation step reports failure and moves on,
leaving a live booking in QueueCare and nothing in the HMS. Together with 4.4,
neither the forward leg nor the compensation leg escalates.

## 8. Open design question for the harness

The two-token split in §7.4 does not fit the data model cleanly.
`hms_integrations` has one credentials blob per row and only a non-unique index
on `(hospital_id, is_active)`, so the natural encoding is **two rows** per
hospital: `fhir_r4` for reads, `generic_rest` for writes.

But `_select_active_direct_integrations()` polls *every* active `direct` row.
A second row means the `generic_rest` adapter also gets polled inbound, where it
ignores `until_date` and would hit `GET /api/appointment` with a user token —
possibly duplicate ingest. Worth resolving before the harness encodes a shape we
then have to unpick.

## 9. Setup facts worth keeping

- Registration rejects a client without `redirect_uris` even for
  `client_credentials`, which never redirects.
- `oauth_app_manual_approval = 0` still leaves `is_enabled = 0` on a new client;
  the scripts flip it directly rather than requiring the admin UI.
- Inline `jwks` registration is broken upstream (openemr#8958), so the key set is
  served by the `jwks` sidecar over the compose network.
- `pc_hometext` is required on appointment create, and omitting it costs you a
  silent 200 (§7.2).
- A local `mysqld` owns 3306; the harness DB port is deliberately unpublished.
