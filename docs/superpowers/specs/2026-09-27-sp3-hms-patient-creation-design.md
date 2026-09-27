# HMS write-back, sub-project 3: staff-approved HMS patient creation and the reconciliation worklist

**Status:** DESIGN, revision 3 (independent review W2R and its re-review W2RR folded in). **All
decision points DECIDED 2026-09-27** (see §11, "Recorded decisions"). Nothing here is built.
**Date:** 2026-09-27
**Tracker:** QueueCare#223 §C
**Repos:** spatiamed-common, QueueCare (server + kiosk), Hospital-portal
**Builds on:**
- `2026-09-10-kiosk-hms-patient-identity.md` (PR #211, unmerged). Stage 2 of that spec is this sub-project.
- `2026-09-11-front-desk-reconciliation-design.md` (PR #211, unmerged): one worklist, attributed,
  countable, no bulk actions.
- SP1 `2026-09-23-hms-write-contract-design.md` and SP2 `2026-09-24-hms-doctor-mapping-design.md`.
- `2026-09-24-dob-first-identity-design.md`, including its "Where this sits" section and the
  `patient_identity_reports` table it created.

## 1. Where this sits

After SP2, a booking writes to the HMS automatically when two things are true: its doctor is mapped,
and its patient has an `ExternalPatientRef`. SP3 covers the patients who have no ref. Afterwards,
every booking either writes automatically or lands in a single staff worklist with a reason.
`HMS_WRITE_BACK_ENABLED` stays off; SP4 turns it on.

### Already decided (binding; not reopened here)

| # | Decision |
|---|---|
| B1 | The worklist holds unmatched patients, ambiguous patients, failed or blocked write-backs (`dead_letter`, `practitioner_unmapped`) and `patient_identity_reports`. Documents come later. |
| B2 | The trigger is booking confirm when the patient has no ref. The strict matcher runs and auto-links a corroborated single match. Anything else queues an item, and **the booking waits**. Kiosk registration also queues items when `HMS_PATIENT_MATCH_ENABLED` is on. *(Read here as: the booking waits only while `HMS_WRITE_BACK_ENABLED` is on, because only then would we write; see DP9.)* |
| B3 | The kiosk shows a neutral cue: "record linked" or "the front desk will complete your registration". It never says "not found". |
| B4 | "Create in HMS" is only for **new** patients. Staff must supply a real DOB and surname. Nothing estimated is ever written to the HMS. |
| B5 | `patients.phone_hash` is globally UNIQUE, and `patients` has no `hospital_id` and no RLS. Households are deferred. |
| B6 | Data migrations are never Alembic, because `entrypoint.sh` stamps head when a migration fails. |
| B7 | Only DigiLocker counts as a document source. |
| B8 | Any `hms_integrations` row on a tenant forces `STAFF_TASK` (`intake.py`, `assignment_policy.py`). |

### Corrections to the brief, found in the code

These change what SP3 has to build. They are recorded here so the work isn't scheduled twice.

1. **The `entries[0]` ambiguity bug is already fixed.** At sm_common v0.14.0 (the pin on QueueCare
   main):
   - `HmsAdapter.search_patients` returns every candidate;
   - `FhirR4Adapter.find_patient` returns `None` when there is more than one;
   - `select_match` consumes the list, so its `ambiguous` branch is live.

   SP3's sm_common work is **create**, **search by name + DOB**, and **search failing loudly**
   (§8). It does not include the ambiguity fix.
2. **`FhirR4Adapter.search_patients` swallows HTTP errors and returns `[]`**
   (`fhir_r4.py`, v0.14.0). An HMS outage therefore looks exactly like "no such patient". Today
   that only causes a missed match. Under SP3, "no candidates" is what unlocks **Create in HMS**,
   so an outage would mint duplicates. It must raise `TransientError` (§8).
3. **"Blocked: no patient ref" has no state today.** `confirm_booking` → `maybe_start_write_back`
   → `enqueue` logs `write_back_skipped reason=no_hms_patient_mapping` and returns `None`. The
   booking is `CONFIRMED` locally, and `write_back_status` stays at its `not_applicable` default.
   That is exactly the false promise ("booked, but the hospital doesn't know") this programme
   exists to remove. So B2 is a **behaviour change on `POST /bookings/{id}/confirm`**, not just a
   new table.
4. **`external_patient_refs` is deliberately many-to-one, but its readers pick at random.** Migration
   076 leaves `(hospital_id, patient_id)` non-unique on purpose: ingest resolves every HMS record on
   a household phone to the one local row (B5) and writes an `ingest_phone` ref per record. Yet
   `enqueue`, `claim_and_attempt` and intake read "the" ref with `.scalars().first()`, so write-back
   can target any household member's chart. SP3 defines one **authoritative** ref per patient and
   makes every reader deterministic (§4.3, DP18).
5. **The kiosk matcher runs only for brand-new patients.** `kiosk.py` returns early for an
   existing phone before `maybe_match_patient`, so returning patients (most of the traffic) are
   never linked at the kiosk. See DP13.
   Worse, the **new-patient** link doesn't persist either: `create_patient_kiosk` runs on
   `get_db_bypassrls` (no auto-commit), and `maybe_match_patient` only `flush()`es the ref after the
   route's last commit (`kiosk.py:220-225`, `hms_patient_match.py:243-253`), so it is rolled back
   when the session closes. Route tests mock the matcher, so nothing catches it. SP3 fixes this
   first (§12, QueueCare task 0).
6. **Identity mismatch is not recorded on the booking.** A `dob_mismatch` report carries
   `booking_id`, but nothing on the booking says "identity unverified". The voice path's
   `create_consultation_request` can auto-assign and write to the HMS **before** it files the
   report (`internal.py:1107-1158`), pushing a household member's booking into the phone-owner's
   chart. SP3 sets the flag at booking insert and gates on it (§3.3).
7. **`get_patient` swallows errors too** (`fhir_r4.py:425-434` returns `None`), so an outage reads
   as "record not found" at Link and in the drawer. It raises `TransientError` in v0.15.0 (§8).

## 2. What SP3 delivers

1. **Link at confirm.** A booking whose patient has no ref runs the matcher at confirm. A
   corroborated single match links the patient and write-back proceeds. Otherwise the booking stays
   `STAFF_TASK` and the patient is put on the worklist.
2. **One worklist** in Hospital-portal. It covers:
   - patient links (unmatched and ambiguous);
   - identity reports;
   - blocked or failed write-backs.

   Each item shows what we have, what the HMS has, and the action that closes the gap. Items are
   attributed and kept after resolution, so the worklist is countable.
3. **Three staff actions on a patient item:**
   - **Search HMS**, live, by phone, by name + DOB, and by MRN;
   - **Link to existing**, which writes a ref with `link_method="staff"`;
   - **Create in HMS** (new patients only), which requires an exact DOB and a real surname, is
     idempotent, and never runs twice. (If the task-1 probe shows OpenEMR drops our marker, the
     one residual is a staff member double-confirming our own earlier record as "not ours",
     §5.1–§5.2.)
4. **A neutral kiosk cue.**
5. **Resolutions for identity reports.** A resolution can release or permanently block that
   booking's write-back.

## 3. Trigger and the confirm contract

This is the load-bearing section.

### 3.1 `POST /bookings/{id}/confirm`

The new step runs **before** any slot is reserved, and only when all of these hold:

- `HMS_WRITE_BACK_ENABLED` is on;
- the tenant has an **active** integration;
- the booking did not originate in the HMS.

When it runs, it does this:

0. **The booking is `identity_unverified`** (§3.3) → checked first. Confirm locally, with no matcher
   run, no item and no 409, and `write_back_status=identity_unverified`. The gate would refuse the
   write whatever the match found, so matching first is wasted work.
1. **Authoritative ref exists** (§4.3) → unchanged. Continue to the normal confirm and enqueue.
2. **No authoritative ref** → run the matcher with the patient's **stored** decrypted name, phone,
   DOB and gender. It has a hard budget of `HMS_MATCH_BUDGET_SECONDS` (new, default 3s). It runs
   inside `get_staff_task_booking_for_update`'s row lock and open transaction, so the 3s is lock
   time too; that is accepted (one booking row, staff-initiated).
   - `matched` → write the ref through the **ref-write rule** (§4.3, `link_method="matcher"`), then
     continue with the normal confirm. Write-back then enqueues as usual. If the rule reports the
     HMS record is linked to another local patient, treat it as a miss with reason
     `linked_to_other_patient`. If it reports `already_linked` (the patient gained a different
     authoritative ref meanwhile, e.g. from ingest), treat it as step 1: re-read the ref and
     continue.
   - `no_match`, `ambiguous`, a timeout or a vendor error → upsert the patient's open item (§4.1)
     and return **409** with `detail="hms_patient_unlinked"` and `{item_id}`.
     - The booking stays `STAFF_TASK`, and nothing is reserved.
     - `write_back_status` becomes `patient_unlinked` (16 chars; the column is `String(20)`), so
       the existing needs-attention filter can find it.
     - **A failure always falls toward the human, never toward a link.**
3. **Phone-less patient** → no dedupe key exists. The endpoint returns the same 409, and the item's
   reason is `no_phone`.

**The item survives the 409.** `get_tenant_session` commits only when the handler returns normally
(`tenant.py:28-30`), so a raised `HTTPException` would roll back the item upsert and the status
write, and the portal would deep-link to an `item_id` that doesn't exist. The route therefore
**returns** the 409 as a `JSONResponse` and lets the dependency commit; it neither raises nor
commits by hand (a manual commit would drop the transaction-local RLS GUC, see §5.3). A test in
§10 pins this.

**A ref from a previous vendor or server counts as no ref.** SP2 D10 wipes the roster and the
doctor map when an integration's `hms_vendor` or `base_url` changes, because ids from one HMS must
never be sent to another. SP3 does the same to patient refs (DP17): they are marked
`superseded_at`, and the §4.3 reader also requires the ref's `hms_vendor` to equal the active
integration's. A superseded ref is invisible to write-back **and to ingest** (the new server may
reuse the old server's ids), so the matcher runs and an item is queued if needed.

With the flag off, confirm behaves exactly as it does today. This keeps staging tenants that
already have an integration (and write-back off) confirming as they do now. See DP9.

Once staff resolve the item (by linking or creating), they confirm again. The portal does this
in-modal (§7), so the usual flow is one sitting. Resolving an item **never auto-confirms** a
booking: confirming is a separate human decision about a slot.

### 3.2 Intake (the auto-assign path)

`intake.py` decides `hms_write_back_ready` before `decide_assignment`. Today, a patient without a
ref makes that decision false, and the booking becomes `STAFF_TASK` (B8).

**Recommended (DP7):** at intake, run the same matcher with a 2s budget, gated the same way as
§3.1, and **skipped when the request is identity-unverified** (§3.3). A match writes the ref
through the ref-write rule, so auto-assign can confirm and write.

Intake runs on a BYPASSRLS session on the voice path (`internal.py:930`, `get_db_bypassrls`), as
the kiosk does (`kiosk.py:499`). RLS is therefore not the safety here: every matcher query and
every `reconciliation_items` read or write on these paths filters or sets `hospital_id`
explicitly. That rule applies to all new SP3 code, whatever the session.

- A miss queues the item. The booking is `STAFF_TASK` (unchanged) with `write_back_status=
  patient_unlinked`.
- Intake is on CareLoop's teleconsult request path, so this adds up to 2s there.
- Telling the caller "a staff member will confirm" is SP4's job (CareLoop branching on `decision`).

### 3.3 Identity gate on write-back

`maybe_start_write_back` gains one more skip condition: **the booking is `identity_unverified`**.

- A new column, `bookings.identity_unverified bool not null default false` (§4.3), is set **when
  the booking is inserted**, from the request's own identity state. It is never set after the
  fact by `file_report`: on the voice path that is too late, because `create_consultation_request`
  may already have auto-assigned and written to the HMS (`internal.py:1107-1158`).
  - Voice: the handler already computes `identity_unverified` before the booking exists
    (`internal.py:1008-1010`). It ORs in DP1's case (an existing patient, no DOB stated), passes
    the result into `create_consultation_request` (a new `identity_unverified` argument), which
    sets the column on insert and forces `allow_hms_write_back=False` (the existing parameter).
    Intake readiness is then false, so there is no auto write **and no DP7 matcher**.
  - `_file_unverified` keeps filing the report with the `booking_id`, but no longer carries the
    gate.
- Such a booking is never written automatically. When `maybe_start_write_back` runs (and only
  after its `HMS_WRITE_BACK_ENABLED` check, so flag-off behaviour is unchanged), its
  `write_back_status` becomes `identity_unverified`, and it shows on the worklist through its
  report.
- The resolution decides what happens next (§6).
- The confirm route applies the same gate. It still confirms locally, because the patient was seen
  and the slot is real, but it does not enqueue a write.

### 3.4 Kiosk registration

When `HMS_PATIENT_MATCH_ENABLED` is on, the kiosk matcher runs as it does today (fire-and-forget,
3s). What changes:

- Its ref now persists: the match is followed by a commit, inside its own savepoint and `try`
  (Correction 5; QueueCare task 0).
- A result that isn't `matched`, other than `skipped` for "no active integration", queues an item
  with origin `kiosk`.
- The item insert sits in its own savepoint. **Registration is never failed by it**; that is the
  existing guarantee.
- A phone-less patient queues a `no_phone` item instead of logging a silent skip.
- The kiosk session is BYPASSRLS too, so the §3.2 explicit-`hospital_id` rule applies.
- The token response carries the cue (§7.2).

## 4. Data model (migration 083, DDL only)

### 4.1 `reconciliation_items` (new, tenant-scoped)

Holds only the item kinds that have no other home: patient links. Identity reports and write-back
jobs already have tables with status and history, and are **not copied** (DP10).

`ENABLE` + `FORCE` RLS with the migration-078 `tenant_isolation` policy. It is added to
`_TENANT_TABLES`, so the boot self-test covers it. Explicit grants follow the 081 pattern.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| hospital_id | uuid not null | RLS key |
| patient_id | uuid not null | FK `patients.id` `ON DELETE CASCADE` |
| kind | text | CHECK in (`patient_unmatched`, `patient_ambiguous`) |
| origin | text | CHECK in (`kiosk`, `booking_confirm`, `intake`) |
| reason | text | CHECK in (`no_candidates`, `several_candidates`, `name_no_corroborate`, `dob_disagrees`, `gender_disagrees`, `mononym_thin`, `no_resource_id`, `search_failed`, `no_phone`, `linked_to_other_patient`) |
| candidate_refs | jsonb not null default `[]` | HMS resource ids only; **no demographics** (DP11). `MatchOutcome` carries no ids today (`hms_patient_match.py:83-87`); it gains `candidate_ids` (a plan task) |
| status | text | CHECK in (`open`, `creating`, `verify_in_hms`, `resolved`, `dismissed`) |
| claimed_by, claimed_at | uuid, timestamptz null | set by the create claim (§5.3) |
| pre_create_ids | jsonb null | HMS ids the search-before-create saw **before our first create POST** (§5.1, §5.3); frozen once `create_posted_at` is set. Ids only, no demographics |
| create_posted_at | timestamptz null | stamped (and committed) immediately before the first create POST for this item; never cleared except by DP17 |
| last_error | text null | sm_common's `_refusal()` string (HTTP status + OperationOutcome issue codes, never body text, `fhir_r4.py:47-63`) or our own error code; never PHI |
| resolution | text null | CHECK in (`linked`, `created`, `created_verified`, `auto_linked`, `dismissed_cannot_resolve_here`, `dismissed_duplicate_local`, `dismissed_not_needed`) |
| external_patient_id | text null | the HMS resource id it was resolved to |
| resolved_by, resolved_at | uuid, timestamptz null | attribution |
| created_at, updated_at | timestamptz not null | |

Constraints:

- **At most one live item per patient per tenant:** a partial unique index on
  `(hospital_id, patient_id)` where `status IN ('open','creating','verify_in_hms')`.
- A second trigger for the same patient refreshes `reason`, `candidate_refs` and `updated_at`
  instead of adding a row.
- SP3 code never deletes rows. That makes the table the countable log. The `ON DELETE CASCADE`
  from `patients` is intended: a patient erasure takes their reconciliation history with it, and
  erasure beats the count.

The item links to the **patient**, not to a booking. Waiting bookings are found as the tenant's
`STAFF_TASK` bookings for that patient with `write_back_status='patient_unlinked'`, and the item
view lists them.

**Auto-close.** If the patient later gains an **authoritative** ref another way (kiosk match,
`ingest_new`, another staff link), the ref-write rule resolves any live item with
`resolution=auto_linked` and `resolved_by=NULL`. Without this, items would linger, and an empty
queue has to look empty. It skips an item under a fresh create claim (§5.3), which resolves itself.
An `ingest_phone` ref closes nothing (DP18).

### 4.2 `patient_identity_reports` changes

These answer Q1, Q2 and Q4:

- **New kinds:**
  - `dob_not_given` (DP1);
  - `name_mismatch` (DP4).

  The CHECK is widened.
- **New columns:**
  - `reported_dob_hash text null`: a salted SHA-256 of `iso|precision` using `HASH_SALT`, the same
    construction as `phone_hash`. AES-GCM v2 uses a random nonce, so ciphertext can't be a dedupe
    key. For an **estimated** HMS DOB (DP5) the input is `estimated|<birth year>` instead, because
    the estimate's `iso` is re-anchored to each clinic day and would never dedupe. When no DOB was
    reported (`dob_not_given`) the input is the sentinel `none`. So `file_report` **always** writes
    a hash for new rows; the column stays nullable only because pre-083 rows have none.
  - `occurrences int not null default 1`.
  - `last_seen_at timestamptz`.
- **Dedupe at filing (DP2):**
  - A partial unique index `uq_patient_identity_reports_open_dedupe` on `(hospital_id,
    patient_id, kind, reported_dob_hash)` where `resolved_at IS NULL AND reported_dob_hash IS NOT
    NULL AND kind IN ('dob_mismatch','name_mismatch','dob_not_given','hms_dob_conflict')`.
  - **Why `reported_dob_hash IS NOT NULL` (W2RR N1).** Today `file_report` never dedupes these
    four kinds (its untargeted `on_conflict_do_nothing()` has only the 082 `dob_reported` index to
    hit, `patient_dob.py:172-187`), and `apply_dob` files `hms_dob_conflict` on **every** mapped
    poll that disagrees (`patient_dob.py:229-235`). So existing tenants already hold several open
    rows per patient and kind, all with a NULL hash. An index keyed on `coalesce(hash,'')` would
    make `CREATE UNIQUE INDEX` raise on them, `entrypoint.sh` would stamp head, and 083 would be
    skipped (B6). Excluding NULL-hash rows makes the build unable to fail, needs no dedupe script,
    and new rows dedupe from day one. The cost: legacy open rows never merge with new ones, so a
    patient may show one legacy row per old filing plus one deduped new row until staff resolve
    them. That is visible, countable, and shrinks to zero as the backlog is worked. (The
    alternative, a guarded `DO` block like §4.3's plus a health field and a dedupe script, keeps
    an extra follow-up alive for no gain.)
  - `file_report` becomes a **targeted** `ON CONFLICT … DO UPDATE SET occurrences = occurrences+1,
    last_seen_at = now()`. This also closes the "untargeted `on_conflict_do_nothing`" Minor in
    #223 §E.
  - The conflict target differs by kind. `dob_reported` keeps its existing index
    (`uq_patient_identity_reports_open_dob_reported`, on `(hospital_id, patient_id)`), and the
    other four kinds use the new one. A Postgres statement takes a single arbiter, so `file_report`
    branches on kind in Python and issues one of two `INSERT … ON CONFLICT` statements.
  - Per-booking gating doesn't depend on the report row: it lives on `bookings.identity_unverified`.
  - A deduped hit returns the **first** row's id, which may already carry another `token_id` or
    `booking_id`. So `link_identity_check` (`patient_dob.py:260-278`) treats an existing open row
    for that patient and kind as linked instead of requiring `token_id IS NULL`, and a resolution
    acts on the patient's flagged bookings, not the report's `booking_id` (§6).
- `resolution` gets a CHECK over the values in §6.

### 4.3 Other DDL

- `bookings.identity_unverified boolean not null default false`.
- `external_patient_refs.superseded_at timestamptz null` (DP17).

**One authoritative ref per patient per tenant.** Migration 076 made `(hospital_id, patient_id)`
many-to-one on purpose (`076_hms_ingest.py:36-39`): ingest links every HMS record on a household
phone to the one local row, as `ingest_phone`. SP3 keeps that and adds a narrower invariant.

- **Authoritative** means `link_method IN HMS_DOB_WRITABLE_LINKS` (`matcher`, `ingest_new`,
  `staff`; `hms_ingest.py:65`) and `superseded_at IS NULL`. That frozenset is the single
  definition; the index predicate, every reader and the kiosk cue use it. `ingest_phone` and
  pre-082 `NULL` refs are dedupe-only (DP18).
- **Index:** `uq_ext_patient_authoritative` on `(hospital_id, patient_id)` where `link_method IN
  ('matcher','ingest_new','staff') AND superseded_at IS NULL`.
- **Readers.** `enqueue` (`hms_write_back_jobs.py:286`), `claim_and_attempt` (`:597`), intake
  (`intake.py:190`), the §3.1 step and `hms_link` all call one helper, `authoritative_ref(db,
  hospital_id, patient_id, integration)`. It filters on authoritative, `hms_vendor` equal to the
  active integration's, and `ORDER BY` staff > matcher > ingest_new, then newest. The ordering only
  matters if the index had to be skipped (below).
- **The ref-write rule** (matcher, staff link, create; §3.1, §3.4, §5.3, §5.4 all use it). Inside a
  savepoint (`begin_nested()`), look up the non-superseded ref for `(hospital_id, hms_vendor,
  external_patient_id)`, and this patient's authoritative ref:
  - the record's ref belongs to **another** patient → no write; outcome `linked_to_other_patient`;
  - this patient already holds an authoritative ref to a **different** record (for example,
    ingest raced a Create) → no write; outcome `already_linked`;
  - the record's ref belongs to **this** patient (typically an earlier `ingest_phone`) → promote
    its `link_method`; outcome `linked`;
  - none → insert; outcome `linked`.

  On `linked`, auto-close the live item (§4.1). A unique violation inside the savepoint is mapped
  to one of the two refusals by constraint name (`uq_ext_patient_external_id` →
  `linked_to_other_patient`, `uq_ext_patient_authoritative` → `already_linked`); it never aborts
  the caller's transaction (today's matcher insert has no savepoint,
  `hms_patient_match.py:243-253`). Callers map the outcomes this way:
  - confirm (§3.1) and intake (§3.2): `already_linked` counts as linked (an authoritative ref now
    exists), so they re-read it and continue; `linked_to_other_patient` is a miss;
  - kiosk (§3.4): `already_linked` counts as linked for the cue; `linked_to_other_patient` queues
    an item;
  - Create (§5.3) and Link (§5.4): both refusals return 409 (`patient_already_linked` or
    `hms_record_linked_to_other_patient`).
- **`uq_ext_patient_external_id`** (076) is recreated as partial on `superseded_at IS NULL`, so a
  new server can reuse an id the old one issued (DP17). Ingest's mapped lookup
  (`hms_ingest.py:154-162`) adds the same filter.
- **Migration 083 never raises on existing data**, as a whole, not just its ext-ref indexes (B6:
  a failed migration makes `entrypoint.sh` stamp head, skipping all of 083, and `_rls_self_test`
  then crash-loops on the missing `reconciliation_items`). Every unique index it builds is
  accounted for, and so is every CHECK it adds to an existing table:
  - `reconciliation_items`' partial index: a new, empty table.
  - The widened `patient_identity_reports.kind` CHECK: dropped and re-added over a strict superset
    of 082's `KINDS`, so every existing row passes.
  - The new `patient_identity_reports.resolution` CHECK: no code writes `resolution` today (only the
    082 column and the model reference it), so existing rows are NULL, which a CHECK accepts. It is
    still added `NOT VALID` so a hand-edited value can't fail the migration; `VALIDATE CONSTRAINT`
    runs later, outside Alembic (B6).
  - `uq_patient_identity_reports_open_dedupe` (§4.2): every existing row has a NULL
    `reported_dob_hash` and so sits outside the predicate.
  - Recreating the 076 index as partial covers a strict subset of rows, so it cannot fail.
  - The authoritative index is built in a `DO $$ … $$` block: if duplicates exist it skips the
    build with `RAISE NOTICE`, otherwise it creates the index (not `CONCURRENTLY`). The `CREATE`
    sits in an inner `BEGIN … EXCEPTION WHEN unique_violation THEN RAISE NOTICE … END`, so an
    authoritative insert by the still-serving old container between the check and the build
    (W2RR N4; practically unreachable) also skips instead of raising. Duplicates are unlikely (an
    `ingest_new` ref exists only for a row ingest created), but not assumed away.
  - Before deploying, a read-only count runs against staging (a plan task): duplicate
    authoritative refs, and open `dob_mismatch` / `hms_dob_conflict` rows per patient and kind
    (informational: it sizes the legacy backlog §4.2 leaves unmerged).
  - After deploying, `/internal/health` reports `ext_ref_unique_index: true|false` from
    `pg_indexes`. If false, a script dedupes (B6) and a later migration repeats the same
    conditional build.
- Multiple active integrations per hospital remain out of scope (SP1).
- `write_back_status` values `patient_unlinked` and `identity_unverified` are **new strings in the
  existing column**. No CHECK exists today, and none is added.

### 4.4 No backfill (DP14)

Existing `CONFIRMED` bookings that have an integration but no ref stay as they are. SP3 applies
going forward. `/internal/health` gains `unlinked_confirmed_bookings`, a count of future
`CONFIRMED` bookings on tenants with an active integration whose patient has no ref, so the gap is
visible before SP4 turns write-back on. Anything that does need a backfill is a script, per B6.

## 5. "Create in HMS"

### 5.1 When it is allowed ("new patient only", B4)

The server enforces all of these; the UI only reflects them:

1. The item is claimable (§5.3): `open`, `verify_in_hms` after a fresh search, or a stale
   `creating`.
2. **Search-before-create, in the same request.** The endpoint re-runs the HMS search with the
   **submitted** demographics, by phone variants and by family name + exact birthdate:
   - A search error returns 503 `hms_search_failed`. **No create happens without a successful
     search** (Correction 2).
   - If any candidate comes back that isn't in `rejected_candidate_ids`, it returns 409
     `candidates_exist` with the candidate refs, and the portal shows them. To create anyway, staff
     must explicitly mark each candidate "not this patient". Their ids are stored in the audit
     event.
   - Until our first create POST for this item (`create_posted_at IS NULL`), each claimed
     request stores this search's ids as the item's `pre_create_ids`. Nothing of ours can exist in
     the HMS yet, so every id it saw is a pre-existing record. Once `create_posted_at` is set,
     `pre_create_ids` is frozen.
   - `PatientCreate.exclude_ids` is `pre_create_ids` plus the ids staff confirmed "not ours" (next
     point). It is **not** every id the current search saw (W2RR N2): after an inconclusive or
     crashed attempt, the current search always sees our own earlier record, and excluding it
     would stop the §5.2 no-marker fallback from ever recognising it.
   - **Possibly our earlier attempt.** When `create_posted_at` is set, any candidate not in
     `pre_create_ids` appeared after we POSTed. The drawer flags it "probably the record from the
     earlier Create attempt" and offers **Link** first. Rejecting it needs a second, explicit
     confirmation, sent as `confirmed_not_ours_ids`; the server refuses (422
     `confirm_not_ours_required`) a rejection of such an id without it. Link on such a candidate
     resolves as `created_verified`, not `linked` (§5.4).
3. **An exact DOB.**
   - `dob_precision` must be `exact`: a real calendar date, validated with
     `sm_common.identity.dob.validate`.
   - `month`, `year` and `estimated` are refused with 422 `dob_not_exact`. OpenEMR stores a DATE,
     so a `year` DOB would be written as a made-up 1 July (DP12).
   - The DOB staff type is applied to the local patient through `apply_dob(source="staff")`, the
     existing precedence path. There is no bypass.
4. **A real surname.**
   - `family_name` is at least 2 characters after trimming, is not all punctuation, and isn't a
     title (the matcher's `_TITLES` set). The ≥ 2 is **our** rule, not a vendor's: a single
     letter is an initial, not the "real surname" B4 requires. (No OpenEMR lname minimum is
     sourced; the rev-1 claim is withdrawn.)
   - A true mononym can't be created. The item is dismissed as `cannot_resolve_here`, and the
     hospital registers the patient by its own process (DP8).
5. **Gender** is required by our form. Whether OpenEMR itself requires sex on create is checked by
   the sm_common task-1 probe. It is `M`/`F`/`O`, mapped to FHIR
   `male`/`female`/`other`.
6. **Phone** is always the patient's stored phone (decrypted server-side), never typed by staff.
   The form shows it masked. A `no_phone` patient is created with no `telecom`, and the
   search-before-create for them uses family name + birthdate only.

The local patient's **name is not changed** by create. There is no staff name-edit path today, and
adding one is out of scope. The HMS record carries exactly what staff typed, and the audit event
records that the typed name differed from the stored one (a boolean, not the values).

### 5.2 Adapter call

`adapter.create_patient(PatientCreate) -> PatientCreateResult`. See §8 for the contract.

- **FHIR:** `POST /Patient` with `name[{text, family, given[]}]`, `birthDate`, `gender`,
  `telecom[{system: phone, value: <E.164>}]`, and
  `identifier[{system: "https://spatiamed.com/patient", value: <patient_id>}]`.
  - `text` is `given + " " + family`. OpenEMR ignores the structured fields and rejects a name
    without `text` ("First Name must not be empty"), and `birthDate` is mandatory (sm_common
    `harness/openemr/FINDINGS.md` §6). How OpenEMR splits `text` into fname/lname is probed in
    sm_common task 1, so the family name staff typed lands in lname; a multi-word surname may need
    a different `text` shape, and the probe decides it.
  - It sends the header `If-None-Exist: identifier=https://spatiamed.com/patient|<patient_id>`.
  - Before POSTing, the adapter searches `Patient?identifier=…`. One hit returns
    `created=False`; several raise `ConflictError`. This is SP1's pattern.
- **Returned id:** the FHIR Patient resource id, the one id kind (SP1 §3A). The ref is written with
  `link_method="staff"`, `mrn` from the created record's identifiers when the vendor returns one,
  and `phone_hash`.
- **OpenEMR:** inherits FHIR create (a system token can `POST /Patient` → 201, FINDINGS).
  **Whether OpenEMR keeps an arbitrary `identifier` on create is unverified.** A live probe is the
  first sm_common task. If it doesn't keep it, `OpenEmrAdapter` overrides the pre-search: the
  marker lookup becomes an exact search on phone variants + family + birthdate (family +
  birthdate for a phone-less patient), and only ids **not in `exclude_ids`** count. One new id is
  "already created" (`created=False`); several raise `ConflictError`. Without the exclusion, twins
  on one phone with one surname and DOB would link twin B to twin A's chart, the record staff had
  just rejected. Because `exclude_ids` holds only pre-POST ids and staff-confirmed "not ours" ids
  (§5.1), our own earlier attempt is never excluded, so the fallback still finds it.
  That fallback is still weaker than a marker: its one residual is a staff member who
  double-confirms our own earlier record as "not ours", which mints a second chart. The §5.1
  flag and second confirmation are the guard; an inconclusive create also never auto-retries
  (§5.3).
- **Other adapters** (`generic_rest`, `bahmni`, `mocdoc`, `csv_import`, `generic_db`) raise
  `WriteNotSupported`. The portal hides Create for their tenants, via a `capabilities.create_patient`
  flag on the worklist summary, and shows "Register in your HMS, then Link".

### 5.3 Idempotency and recovery

This follows SP1's discipline: a DEFINITIVE outcome versus an INCONCLUSIVE one.

1. **Claim.** `UPDATE … SET status='creating', claimed_by, claimed_at WHERE id=:id AND
   hospital_id=:hid AND (status IN ('open','verify_in_hms') OR (status='creating' AND claimed_at <
   now() - interval '5 min'))`, then commit. Zero rows updated returns 409 `create_in_progress` or
   `already_resolved`. This is what makes a double-click, two receptionists, or a retry safe.
   - **RLS after a commit.** The tenant GUC is transaction-local (`tenant.py:23-26`), so every
     manual commit in this endpoint is followed by re-setting it, as `assignment.py:486-496` does.
     Without it the next transaction's INSERT fails the policy's `WITH CHECK` and its SELECTs see
     nothing. The tests for this run as `app_user`, not a BYPASSRLS role.
2. **Search, then create**, outside any open transaction, with a budget of
   `HMS_CREATE_BUDGET_SECONDS` (default 10s). Between the search and the POST, one short
   transaction writes `pre_create_ids` (if still unfrozen) and stamps `create_posted_at` (if null),
   and commits (GUC re-set after). A crash after the POST therefore always leaves the stamp.
3. **Outcome:**
   - **Created, or found by the marker** → in one transaction (GUC re-set first):
     - write the ref through the ref-write rule (§4.3);
     - `apply_dob(staff)`;
     - set the item to `resolved`, with resolution `created` (or `created_verified` when the marker
       found an earlier attempt);
     - write an `AuditEvent`;
     - commit.
     - If the ref write reports `linked_to_other_patient`, return 409
       `hms_record_linked_to_other_patient`; if `already_linked`, return 409
       `patient_already_linked` (the same codes as Link, §5.4). The HMS record exists, and the
       audit event says so. With the claim respected by Link and auto-close (below), this needs a
       race with ingest to happen.
   - **Definitive refusal** (a 4xx validation error, `WriteNotSupported`, `AuthError`) → back to
     `open`, with `last_error` (§4.1). Staff can fix the input and retry.
   - **Inconclusive** (a timeout, 5xx or 429 once the POST has started, or a crash) → status
     `verify_in_hms`. **This state never retries automatically.** Staff press **Search HMS**; a hit
     on the marker, or a candidate flagged as possibly our earlier attempt (§5.1), is linked as
     `created_verified`. Create is offered again only
     after a search in the same request returns nothing (§5.1).
4. **Stale claim.** A `creating` row with `claimed_at` older than 5 minutes (a process crash) is
   shown as `verify_in_hms` and is claimable by the predicate above, whose search-before-create
   then finds an earlier attempt by its marker (without a marker: flags it as possibly ours,
   §5.1). No background worker is needed.
5. **A fresh claim blocks the rest.** While `status='creating' AND claimed_at > now() - 5 min`,
   Link, Dismiss and auto-close leave the item alone (Link and Dismiss return 409
   `create_in_progress`), so a second receptionist or a confirm-time match can't race a create
   that is already in flight.

### 5.4 Link to existing

`POST …/link {external_patient_id}`:

- The id must be one the server just found in **this request's** live search, or in the item's
  `candidate_refs`. Staff can't type arbitrary ids.
- The server calls `get_patient` to confirm the record exists. An outage raises `TransientError`
  (v0.15.0) and returns 503, not "not found".
- It refuses with 409 `create_in_progress` under a fresh create claim (§5.3).
- It writes the ref through the ref-write rule (`link_method="staff"`) and resolves the item as
  `linked`, or as `created_verified` when `create_posted_at` is set and the id is not in
  `pre_create_ids` (§5.1). An existing `ingest_phone` ref for this patient and record is promoted.
- If the patient already holds a different authoritative ref (`already_linked`), it returns 409
  `patient_already_linked`.
- If that HMS record is already linked to a **different** local patient, it returns 409
  `hms_record_linked_to_other_patient`. This is the household case:
  one phone per local row (B5) means staff can't fix it here, and the item can be dismissed as
  `cannot_resolve_here`.
- The `CanonicalPatient` that `get_patient` just returned is applied at once through
  `apply_dob(source="hms")`, under the existing precedence rules (a `staff` link is DOB-writable).
  There is no need to wait for the next appointment ingest.

## 6. Identity reports on the worklist

| kind | Staff actions (resolution) | Effect |
|---|---|---|
| `dob_mismatch` | `same_person_dob_confirmed` (staff enter the correct exact DOB) · `same_person_no_change` · `different_person` · `dismissed` | "same person" clears `identity_unverified` on every open booking of that patient at that tenant that has it set (not only the report's `booking_id`; reports are deduped, DP2). It does not auto-enqueue; the portal offers **Retry write-back** (see Retry below). `different_person` sets those bookings' `write_back_status=manual_required` with "household member: enter in HMS by hand", permanently, because households are deferred (B5). |
| `dob_not_given` (new, DP1) | same set | same |
| `name_mismatch` (new, DP4) | `same_person_name_variant` · `different_person` · `dismissed` | as above |
| `dob_reported` | `accept` (writes `staff` DOB via `apply_dob`) · `reject` | DOB only; no booking effect |
| `hms_dob_conflict` | `keep_ours` · `take_hms` (`apply_dob(staff)` with the HMS value) · `dismissed` | DOB only |

"Those bookings" in the table always means that set. Every resolution writes `resolved_at`,
`resolved_by` and `resolution`, plus an `AuditEvent`.

### Write-back items

Write-back items are read live from `hms_write_back_jobs` and the booking:

- `dead_letter` jobs;
- bookings with `manual_required`, `patient_unlinked` or `identity_unverified`.

The actions are:

- **Retry** calls `maybe_start_write_back` / `enqueue`. That inserts a fresh job, or REVIVEs a
  `dead_letter` one, so it covers both shapes. A booking that was `identity_unverified` or
  `patient_unlinked` never had a job, so there is nothing to revive there. The action is audited as
  `hms_write_back_revived`. It is allowed only when the cause is gone: the doctor is mapped (SP2),
  the patient is linked, and the booking isn't `identity_unverified`.
- **Mark entered in HMS by hand**: a new `write_back_status=manual_entered`, audited, attributed.
  This closes the item without claiming we wrote anything.
- `practitioner_unmapped` items deep-link to SP2's Doctors section.

## 7. Surfaces

### 7.1 Hospital-portal: the worklist

- **Where:** a "Worklist" tab in `modules/hms` (next to `ReviewLaterInbox` on `HmsDashboardPage`),
  with an open-count badge. It is visible when the tenant has an integration or has any identity
  report.
- **List:** one row per item from all three sources, in one response shape:
  `{source, id, kind, reason, patient: {name, masked_phone, age_label}, bookings[], created_at,
  status, occurrences}`.
  - Filters: kind and status.
  - Default sort: oldest open first.
  - No bulk selection (base spec).
- **Summary strip:** open items by kind, and resolved this month by resolution. This is the
  "47 not in your chart" number, served by `GET /reconciliation/summary`.
- **Patient-item drawer:**
  - our side (name, DOB with its precision, gender, masked phone);
  - **Search HMS** results, rendered side by side with the differing fields highlighted;
  - **Link** per candidate;
  - **Create in HMS** as a form with family name, given names, an exact DOB (DD/MM/YYYY, reusing
    `src/lib/dob.ts`), gender, and "none of these is this patient" ticks;
  - **Dismiss** with a reason.

  Opening the drawer or searching writes a `PhiAccessLog` row. So does each list read: one row
  per request, carrying the patient ids returned, because the list decrypts names.
- **Confirm-flow integration:** confirm lives in `DoctorSlotPicker.tsx` (`:37-66`);
  `ReviewLaterInbox` has no confirm of its own. When confirm returns 409 `hms_patient_unlinked`,
  `DoctorSlotPicker` opens that item's drawer inline, and on resolution offers "Confirm now" with
  the same doctor and slot. It re-validates the slot, since nothing was reserved.
- `src/lib/access.ts` mirrors the new permissions, and `gen:schemas` regenerates types after
  QueueCare deploys.

### 7.2 QueueCare kiosk: the neutral cue

- The **token response** (what the token screen renders) gains `hms_link: "linked" | "pending" |
  null`. It is non-null only for a patient registered new in this kiosk session, or a returning
  patient after `verify-dob` returned `verified`; the plan confirms how the token request carries
  that state, and if it can't, returning patients get `null`. Emitting `linked` for a typed,
  unverified phone would reveal that the phone's owner is in the HMS, which the DOB spec's R5
  rules out. Its states:
  - `linked`: the patient has an **authoritative** ref at this tenant (§4.3; it existed already,
    or was matched just now);
  - `pending`: a live item exists for the patient;
  - `null`: no HMS on this tenant, the flag is off, or the answer is unknown. **Nothing is shown**
    for `null`.
- **Token screen** (`app/token/[tokenId].tsx`) shows a quiet line under the token:
  - `linked` → "Your hospital record is linked";
  - `pending` → "The front desk will complete your registration".
- It never shows "not found", candidate counts or any HMS data.
- **Offline** registrations show nothing, because the state isn't known.
- New strings go in all 8 languages (`strings.ts:506`: en, hi, mr, ta, te, kn, ml, ar) through the
  existing process.
- `hms_link` is computed from our own tables only. A kiosk can't use it to probe the HMS for a
  returning patient: the answer is the same whatever the HMS says.
- **Accepted residual.** For a **new** patient, `linked` means the matcher just corroborated the
  typed name (and DOB) against an HMS record on that phone, so it is a narrow membership oracle for
  a typed name + phone. B3's cue makes this inherent; it is recorded, not fixed.

## 8. spatiamed-common changes (v0.15.0)

1. **`search_patients` and `get_patient` raise `TransientError`** on an HTTP or transport error,
   instead of returning `[]` / `None` (`fhir_r4.py:390-395`, `:425-434`). A 404 from `get_patient`
   still returns `None`. This is a behaviour change, and its callers are:
   - QueueCare `hms_patient_match` already swallows it at the kiosk; at confirm it becomes a
     `search_failed` item;
   - `hms_ingest.py:110-112` (`get_patient`, then the `find_patient(mrn=…)` fallback) is **already
     covered**: the per-appointment `except Exception` counts it as `failed` inside its savepoint
     (`hms_ingest.py:365-371`), so no patient is created without HMS data. The plan pins that with
     a test; no code change.
   - Link (§5.4) and the drawer return 503 instead of "not found".
   - `find_patient` inherits the change.
2. **`search_patients` gains `family: str | None` and `birth_date: date | None`.** FHIR:
   `Patient?family=…&birthdate=eq<YYYY-MM-DD>`, always used together. A name-only search is refused
   (a `ValueError`), because it returns half a town. This is how staff find a record whose phone
   differs.
3. **`HmsAdapter.create_patient(PatientCreate) -> PatientCreateResult`** on the base contract. It is
   **not** abstract. The default raises `WriteNotSupported`, so the five non-FHIR adapters need no
   change.
   - `PatientCreate(patient_marker: UUID, family: str, given: list[str], birth_date: date, gender:
     str, phone: str | None, exclude_ids: frozenset[str])` is validated in `__post_init__`:
     `family` ≥ 2 characters, `birth_date` a real date. There is no precision field, because a
     `date` is exact by construction. `exclude_ids` is the item's `pre_create_ids` plus staff-confirmed "not ours" ids (§5.1, §5.2 fallback).
   - `PatientCreateResult(resource_id: str, mrn: str | None, created: bool)`.
   - Errors follow SP1: `ConflictError` (several marker hits), `TransientError`, `AuthError`,
     `VendorRejected`, `WriteNotSupported`. `created=True/False` works as in `WriteBackResult`.
4. **`FhirR4Adapter.create_patient`**, as in §5.2. `OpenEmrAdapter` override only if the identifier
   probe fails.
5. **Live harness** `harness/openemr/verify_patient_create.py` (marked `live`), with steps (a)–(d):
   - (a) create returns 201 and a resource id;
   - (b) a second create with the same marker gives `created=False` and the same id, and a direct
     DB count shows one row;
   - (c) the record reads back with the name, DOB, sex and phone;
   - (d) whether the identifier persists, recorded in `FINDINGS.md`.

### Release order

1. sm_common v0.15.0: PR, tag.
2. Bump the QueueCare pin in **both** `server/` and `notification_service/` (with its
   `requirements.txt` mirror), together. This is QueueCare task 1 (§12): server tasks 4 and 8
   need v0.15.0.
3. QueueCare server: migration 083, then the APIs. Before deploy, run the ref duplicate count;
   after deploy, check `/internal/health` (`ext_ref_unique_index`).
4. Hospital-portal: `gen:schemas` against the deployed staging, then the worklist.
5. Kiosk build with the cue.

CareLoop needs no pin bump for SP3. The kiosk and portal changes are additive: an old kiosk ignores
`hms_link`, and an old portal just sees a 409 with a readable `detail` on confirm.

## 9. Permissions, audit, privacy

**Permissions (DP16):**

- `reconciliation.view` lists and reads items.
- `reconciliation.resolve` covers link, dismiss, identity-report resolutions, write-back
  retry, and mark-entered.
- `reconciliation.create_hms_patient` is **Create in HMS** alone. Creating a record in the
  hospital's system of record is a different authority from linking to an existing one.

Role grants:

| Role | view | resolve | create_hms_patient |
|---|---|---|---|
| receptionist | explicit | explicit | explicit |
| nurse | explicit | explicit | no |
| hospital_admin, admin, super_admin | via `*` | via `*` | via `*` |
| doctor | no | no | no (base spec: never mid-consultation) |
| dept_head, billing | no | no | no |

None of these fall under `patient.*`, so the receptionist grant must be explicit.

**Audit:** every mutating action writes an `AuditEvent` with actor and time:

- `reconciliation_item_queued`
- `hms_patient_auto_linked`
- `hms_patient_linked`
- `hms_patient_created` (carries `created`, the rejected candidate ids, the `confirmed_not_ours_ids`,
  and `name_differs_from_local`)
- `hms_patient_create_inconclusive`
- `reconciliation_item_dismissed`
- `identity_report_resolved`
- `hms_write_back_revived`
- `write_back_marked_manual`

**PHI:**

- HMS candidate demographics are **never stored**, only resource ids (DP11). They are fetched live
  with `get_patient` when the drawer opens, and each view or search writes `PhiAccessLog`.
- Logs carry ids, kinds, reasons and outcomes: never names, DOBs or phones.
- The worklist list masks the phone; the full phone is never sent to the portal. Each list read
  writes `PhiAccessLog` (§7.1).

## 10. Testing

Every fix-carrying test must fail when its fix is reverted (programme rule since 2026-09-16).

**sm_common (respx):**
- `search_patients` raises on 5xx or transport error, and on a name without a DOB;
- `family`+`birthdate` query shape;
- `get_patient` raises on 5xx or transport error, and still returns `None` on 404;
- create carries the marker identifier, `If-None-Exist` and `name[].text`;
- the OpenEMR fallback pre-search ignores `exclude_ids`, and several new ids → `ConflictError`;
- a pre-search hit → `created=False` with no POST;
- several hits → `ConflictError`;
- `PatientCreate` rejects a 1-character surname;
- the default `create_patient` → `WriteNotSupported`.

**QueueCare (real Postgres, `uv sync --extra dev`, bare `pytest`):**
- Confirm, with the flag on:
  - no ref + matched → ref + CONFIRMED + job enqueued;
  - no ref + no_match, ambiguous, timeout or vendor error → 409, `STAFF_TASK`, nothing reserved,
    one item, and **the item row and `patient_unlinked` status persist after the 409**;
  - a ref whose `hms_vendor` differs from the active integration's is treated as no ref, both at
    confirm and in `enqueue`;
  - a second confirm refreshes the same item, not a new one;
  - a match whose HMS record already has an `ingest_phone` ref for this patient promotes it (no
    500); one held by another patient → 409 + `linked_to_other_patient`;
  - flag off → today's behaviour, byte-identical.
- Intake: a match lets auto-assign confirm; a miss → `STAFF_TASK` + item.
- Identity gate:
  - **an unverified voice booking with write-back on makes zero adapter calls** (no write, no DP7
    match), and the column is set on the inserted row;
  - an `identity_unverified` booking never enqueues; "same person" clears it on every flagged
    booking of the patient, and retry enqueues; `different_person` → `manual_required`;
  - with the flag off, `write_back_status` is unchanged.
- Authoritative ref: a household of six `ingest_phone` refs yields no write-back target; the three
  readers pick staff > matcher > ingest_new; ingest keeps working for the 2nd..Nth household
  member.
- Create:
  - claim race (two concurrent requests, one wins);
  - claim → commit → create → ref insert succeeds as `app_user` (GUC re-set after each commit);
  - a stale `creating` claim is reclaimed; Link and Dismiss under a fresh claim → 409;
  - OpenEMR fallback: a hit on a pre-POST or staff-confirmed id is not "already created" (twins);
  - our own earlier record (inconclusive first attempt, marker dropped) is **not** in
    `exclude_ids` on the retry: the fallback returns `created=False`, and the item resolves
    `created_verified`; rejecting that candidate without `confirmed_not_ours_ids` → 422;
  - a crash after the POST leaves `create_posted_at` set; Link on a post-POST candidate →
    `created_verified`;
  - search failure → 503 and no vendor create call;
  - an unrejected candidate → 409;
  - non-exact DOB → 422;
  - mononym → 422;
  - inconclusive → `verify_in_hms` with no automatic retry;
  - marker hit → `created_verified`;
  - staff DOB goes through `apply_dob` precedence.
- Link: an id not from a live search → 422; an HMS record linked to another patient → 409;
  `get_patient` outage → 503.
- Migration 083 over seeded duplicates: completes, skips the authoritative index with a NOTICE,
  and `/internal/health` reports `ext_ref_unique_index: false`. Without duplicates, a racing link
  and create leave one authoritative ref.
- Migration 083 over seeded duplicate open `dob_mismatch` and `hms_dob_conflict` rows (same
  patient and kind, NULL hash): completes, and builds `uq_patient_identity_reports_open_dedupe`.
- Ref-write rule: a patient already holding a different authoritative ref → `already_linked`, no
  write; confirm treats it as linked and continues.
- Confirm with the flag on and an `identity_unverified` booking with no ref: zero adapter calls,
  no item, CONFIRMED locally.
- `file_report` dedupe: the same wrong DOB twice → one row, `occurrences=2`; a different wrong DOB
  → a second row; `dob_not_given` twice → one row (sentinel hash); a second kiosk `dob_mismatch`
  still links to its token.
- DP5: an estimated HMS DOB on a mapped refresh never writes, and polls on successive days file at
  most one `hms_dob_conflict`.
- DP17: after a vendor or `base_url` change, refs are superseded; ingest doesn't resolve a reused id
  to the old patient; live items' `candidate_refs` are cleared.
- Kiosk (task 0): the matcher's ref is readable through a **fresh** session after the register
  route returns (no mocked matcher).
- Auto-close on a ref written by kiosk or ingest.
- RLS isolation for `reconciliation_items` (the boot self-test plus a cross-tenant read test).
- `ingest` survives `search_patients` raising.
- The kiosk register and token `hms_link` in all three states, and registration succeeds when the
  item insert fails.
- Permissions: a doctor → 403; a nurse can't create.

**Kiosk:** the cue renders for `linked` and `pending`, and nothing for `null` or offline; i18n keys
exist in all 8 languages.

**Portal:** the drawer's search → link and create flows (MSW); the `candidates_exist` path forces
explicit rejection; the 409-on-confirm inline flow; the summary counts.

**Live OpenEMR round trip (the done criterion).** This uses the harness OpenEMR 8.3.0 and local
QueueCare code, with `HMS_WRITE_BACK_ENABLED=true` in the **local test process only** (never on
staging; SP4 owns that):

1. Kiosk-register a patient who isn't in OpenEMR. The item is `patient_unmatched`, and the cue is
   `pending`.
2. Confirm a booking for them → 409.
3. Create in HMS from the worklist API with an exact DOB. OpenEMR has one new patient, with the
   right name, DOB, sex and phone.
4. Repeat the create request → 409 `already_resolved`. The OpenEMR count is still 1.
5. Confirm again → CONFIRMED, and write-back lands the appointment against the **new** patient's
   `pid` with the mapped practitioner (SP2).
6. The ingest loop re-reads the appointment and doesn't re-import it.
7. The ambiguity seed (six patients, one phone) yields a `patient_ambiguous` item with six
   `candidate_refs`, and never an auto-link.

## 11. Decision points

Items marked "open Q" are the six open questions from #223 §C.

### Recorded decisions (user, 2026-09-27; binding)

| DP | Decision |
|---|---|
| DP1 | **DECIDED (b)**: file `dob_not_given` and set `identity_unverified` at booking insert. |
| DP2 | **DECIDED (a)**: dedupe at filing (`reported_dob_hash`, partial unique index, `occurrences`). |
| DP3 | **DECIDED**: out of SP3 scope; CareLoop follow-up. |
| DP4 | **DECIDED (b)**: new `name_mismatch` kind. |
| DP5 | **DECIDED (b), compare-only**: ingest compares an estimated HMS DOB, never writes it. |
| DP6 | **DECIDED**: out of SP3 scope (own kiosk project). |
| DP7 | **DECIDED (b)**: also match at intake, 2s budget. |
| DP8 | **DECIDED (a)**: refuse a mononym; dismiss `cannot_resolve_here`. |
| DP9 | **DECIDED (a)**: `HMS_WRITE_BACK_ENABLED` (+ an active integration) gates the confirm change. |
| DP10 | **DECIDED (b)**: store only patient-link items; build the list over three sources. |
| DP11 | **DECIDED (b)**: store resource ids only; fetch demographics live. |
| DP12 | **DECIDED (a)**: `exact` DOB only to create. |
| DP13 | **DECIDED (b)**: also match returning kiosk patients after `verify-dob` returns `verified`. |
| DP14 | **DECIDED**: no backfill; `/internal/health` shows the gap. |
| DP15 | **DECIDED (a)**: no slot hold; confirm re-validates. |
| DP16 | **DECIDED (b)**: `reconciliation.view` / `.resolve` / `.create_hms_patient`; nurse resolves but cannot create. |
| DP17 | **DECIDED (a)**: supersede refs on a vendor or `base_url` change; superseded refs are invisible to ingest too. |
| DP18 | **DECIDED (b)**: only `matcher`, `ingest_new` and `staff` refs are authoritative. |

Every recommendation below stands as written; the options are kept for provenance.

**DP1 (open Q1): a "don't know" DOB from a returning caller or kiosk patient.**
Options:
- (a) Book silently, as today.
- (b) File a `dob_not_given` report and set `identity_unverified` on the booking.
- (c) Refuse the booking.

**Recommend (b).** The phone's row may be a different household member. With no DOB we have no
evidence either way, so the DOB spec's rule "stating sources never change identity; record, don't
guess" applies. It costs nothing when there is no HMS, because the gate only blocks write-back.
Reports are deduped (DP2), so a regular "don't know" patient makes one open row. *Refined:* the
booking flag is set at booking insert, not when the report is filed (§3.3).

**DP2 (open Q2): duplicate `dob_mismatch` rows.**
Options:
- (a) Dedupe at filing (a keyed hash of the reported DOB, a partial unique index on open rows,
  `occurrences`).
- (b) Group at read time in the worklist.

**Recommend (a).** The table stays small and countable, `occurrences` keeps the signal, and it fixes
the untargeted `on_conflict_do_nothing` Minor. Per-booking gating moves to
`bookings.identity_unverified`, so it no longer depends on one report per booking. A **different**
wrong DOB still files a separate row, and that is a real signal. *Refined:* a resolution acts on
all the patient's flagged bookings, and `link_identity_check` treats a deduped hit as linked
(§4.2).

**DP3 (open Q3): the realtime-voice attempt cap is enforced by the prompt.**
**Recommend: out of SP3 scope.** Track it as a CareLoop follow-up. The server-side `verify-dob` cap
(5 per phone per 24h, fail-closed) already bounds it, and every extra attempt ends as
`identity_unverified`, which SP3's gate now keeps out of the HMS. The residual is conversational
(extra turns), not an identity leak.

**DP4 (open Q4): a DigiLocker DOB that agrees while the names don't.**
Options:
- (a) Keep filing `dob_mismatch`.
- (b) Add a new `name_mismatch` kind.

**Recommend (b).** Staff do something different for it: confirm a name variant, not a DOB. Filing
it as a DOB mismatch sends staff to check the wrong field.

**DP5 (open Q5): an age-only HMS no longer files `hms_dob_conflict` on a mapped refresh.**
`hms_ingest.py:171` skips `estimated` HMS DOBs entirely.
Options:
- (a) Leave it.
- (b) Pass the estimate to `apply_dob`, which never lets an estimate overwrite a stated DOB and
  files a conflict only when `corroborates(...)` is `False` (outside ±2 years).

**Recommend (b), CHANGED in mechanism:** compare only, never write. Passing the estimate to
`apply_dob` would rewrite a stored `hms` estimate at equal rank on every poll, because it is
re-anchored to each clinic day (`hms_ingest.py:165-171`, `patient_dob.py:226-230`), and its
changing `iso` would defeat the dedupe. So ingest compares the estimate against the stored DOB
with `corroborates(...)`, and on `False` files `hms_dob_conflict` keyed on `estimated|<birth
year>` (§4.2). A disagreement of more than 2 years with the hospital's own record is exactly what
staff should see. "Can't confirm" still files nothing.

**DP6 (open Q6): the kiosk has no DigiLocker/Aadhaar/ABHA decode path.**
**Recommend: out of SP3 scope**, as its own kiosk project together with the broken QR decoder.
SP3 doesn't need it: staff enter the exact DOB at create, and the worklist's Link action is the
mitigation. Nothing in SP3 depends on document attestation at the kiosk.

**DP7: run the matcher at intake (auto path) as well as at staff confirm?**
Options:
- (a) Staff confirm only.
- (b) Also at intake, with a 2s budget.

**Recommend (b).** Readiness is decided at intake, so without it every unlinked patient's booking
becomes a staff task even when a clean match exists. The cost is up to 2s on CareLoop's request
path. SP4's `decision` branching tells the caller. *Refined:* skipped for an identity-unverified
request (§3.3), `hospital_id` filtered explicitly on the BYPASSRLS session (§3.2), and the ref
written through the savepointed ref-write rule (§4.3).

**DP8: a true mononym at Create in HMS.**
Options:
- (a) Refuse; the item is dismissed `cannot_resolve_here`, and the hospital registers them itself.
- (b) Let staff type the hospital's padding convention.
- (c) A per-integration padding setting.

**Recommend (a).** B4 says "real surname", and padding is a hospital policy we shouldn't encode.
Revisit with (c) if a trial hospital asks.

**DP9: which flag gates the confirm behaviour change?**
Options:
- (a) `HMS_WRITE_BACK_ENABLED` (plus an active integration).
- (b) A new `HMS_RECONCILIATION_ENABLED`.

**Recommend (a).** Blocking a confirm only matters when we'd write, so tenants with an integration
and write-back off keep today's confirm. The worklist UI and Create in HMS work whenever there's an
active integration. That lets staff clear the queue on a trial tenant **before** SP4 turns
write-back on.

With (a), the three switches work like this:

| Switch | What it turns on |
|---|---|
| `HMS_WRITE_BACK_ENABLED` + an active integration | Matching at confirm and intake, the 409 wait, and the identity gate |
| `HMS_PATIENT_MATCH_ENABLED` + an active integration | Kiosk matching, kiosk item queueing, and the `hms_link` cue |
| An active integration (any flag) | The worklist, Search, Link and Create in HMS |

Identity reports show on the worklist whatever the flags, because they exist regardless.

**Ships regardless of `HMS_WRITE_BACK_ENABLED`** (visible on deploy, so called out):

- migration 083: the new table, columns, the authoritative index and the partial 076 index;
- `file_report` dedupe and the `link_identity_check` change (DP2);
- `dob_not_given` reports (DP1) and `bookings.identity_unverified` set at insert (the
  `write_back_status` string is not; it waits for the flag);
- DP5's compare-only ingest filing `hms_dob_conflict`;
- ingest ignoring superseded refs, and `reset_roster` superseding refs (DP17);
- sm_common `search_patients` / `get_patient` raising (reaches the kiosk matcher and ingest today);
- under `HMS_PATIENT_MATCH_ENABLED`: the kiosk ref commit fix (task 0), kiosk queueing, DP13 and
  the `hms_link` cue;
- under any active integration: the worklist, Search, Link and Create in HMS;
- the `/internal/health` fields.

Flag-gated and invisible with it off: the confirm-time matcher and 409, the intake matcher, the
identity gate's status write, and the authoritative-ref readers in `enqueue` and intake (both
return early without the flag).

**DP10: worklist storage.**
Options:
- (a) Copy everything into one `reconciliation_items` table, as the base spec says.
- (b) Store only patient-link items, and build the list at read time over three sources.

**Recommend (b).** Identity reports and write-back jobs already carry status, resolution and history.
Copying them invites the two copies to drift. One response shape keeps "one queue" for staff.

**DP11: store HMS candidate demographics on ambiguous items?**
Options:
- (a) Store them.
- (b) Store only resource ids and fetch live.

**Recommend (b).** It avoids a second, unowned copy of the hospital's PHI, and the drawer always
shows current data. The cost is one `get_patient` per candidate when the drawer opens, which must
raise on an outage rather than show "not found" (§8).

**DP12: DOB precision required to create.**
Options:
- (a) `exact` only.
- (b) Also `month`/`year` where the vendor accepts partial `birthDate`.

**Recommend (a).** It holds for every vendor. OpenEMR's DATE column would turn a `year` into a
made-up 1 July, which is "estimated" in disguise and breaks B4.

**DP13: match returning kiosk patients too?** Today only new patients are matched.
Options:
- (a) New only.
- (b) Also returning patients with no ref, but only after `verify-dob` returns `verified`.

**Recommend (b).** Returning patients are most kiosk traffic. Gating on `verified` means we never
link a household member who is using the phone owner's row. *Refined:* depends on task 0 (the
kiosk link otherwise never persists), and matches on the **stored** name and DOB, not what was
typed: the link describes the row, not whoever is at the kiosk.

**DP14: backfill links for existing confirmed bookings?**
**Recommend: no.** SP3 applies going forward only, and `/internal/health`'s
`unlinked_confirmed_bookings` makes the gap visible. Prod has no HMS tenants; staging's are test
data.

**DP15: hold the slot while a booking waits on a patient link?**
Options:
- (a) No hold; resolve inline, then confirm re-validates the slot.
- (b) Pin the chosen slot until the item resolves.

**Recommend (a).** Resolution is usually seconds, in the same modal. A pin would need an expiry, and
SP1 made unexpiring pins mean "an HMS may hold this", a meaning we shouldn't overload.

**DP16: permission split.**
Options:
- (a) One `reconciliation.resolve` (base spec).
- (b) Add `reconciliation.create_hms_patient` (and `.view`), with nurse getting resolve but not
  create.

**Recommend (b).** Creating in the system of record is a different authority. A split lets a
hospital restrict it without taking away the rest of the worklist. `dept_head` and `billing` get
none of the three (§9).

**DP17: a `base_url` change on the same vendor.**
§3.1 treats a ref from a different `hms_vendor` as absent, but a new `base_url` for the same vendor
(a new hospital server) keeps the old ids, and those ids would be sent to the new server.
Options:
- (a) On any vendor or `base_url` change, mark the integration's refs stale, with a
  `superseded_at` column and an audit event, the way SP2 D10 wipes the doctor map. Stale refs count
  as absent, and ingest keeps them for dedupe.
- (b) Store `hms_integration_id` on new refs, and compare against it.
- (c) Leave it; a vendor change is rare and done by hand.

**Recommend (a).** It mirrors D10's rule in the same transaction (`reset_roster`,
`hms_integrations.py:151-172`), and a stale ref costs one match. **CHANGED in detail:** rev 1 said
ingest keeps superseded refs for dedupe. It must not: a non-UUID vendor's new server can issue an
id the old one used, and ingest would resolve it to the old server's patient. So superseded refs
are invisible to ingest too, `uq_ext_patient_external_id` becomes partial on `superseded_at IS
NULL` (§4.3), and the same transaction clears `candidate_refs`, `pre_create_ids` and
`create_posted_at` on live items (they describe the old server) and sets their reason to
`no_candidates` until the next search.

**DP18 (new, from the review): does an `ingest_phone` ref count as a write-back ref?**
Today any ref does; `enqueue` takes `.first()`. On a household phone, ingest writes one
`ingest_phone` ref per HMS record onto the single local row (B5), so "the" ref may be another
member's chart.
Options:
- (a) Yes, as today.
- (b) No: only `matcher`, `ingest_new` and `staff` refs are authoritative (§4.3). An
  `ingest_phone`-only patient goes through the matcher, which links a single corroborated match
  (promoting that ref) or files an ambiguous item.

**Recommend (b).** A phone hit is not identity; the existing DOB rule already treats
`ingest_phone` as too weak to write a DOB (`HMS_DOB_WRITABLE_LINKS`), and write-back to the wrong
chart is worse than a DOB. The cost is one match for such patients. This is also what lets §4.3
have a unique index without breaking ingest.

**(a) is not a drop-in alternative.** §4.3's `uq_ext_patient_authoritative`, the auto-close rule and
the `hms_link: linked` cue all assume (b). Choosing (a) means **no** authoritative index (a household
patient legitimately holds several refs), and readers that rely on ordering alone, which can still
pick a household member's chart. The spec would need those three parts reworked, not just a
different frozenset.

## 12. Task breakdown (rough; the plan will refine it)

**spatiamed-common (→ v0.15.0)**
1. Live probes, recorded in FINDINGS:
   - Does OpenEMR keep a FHIR Patient `identifier` on create?
   - How does OpenEMR split `name[].text` into fname/lname (single- and multi-word surnames)?
   - Does OpenEMR require sex on create?
   - Does OpenEMR's FHIR `Patient` search honour `family` + `birthdate`? §8 item 2 depends on it,
     and the CapabilityStatement is known to lie.
2. `search_patients` and `get_patient` raise `TransientError`; `family`+`birth_date` search.
3. `PatientCreate` (with `exclude_ids`) / `PatientCreateResult`, and the base `create_patient`
   default.
4. `FhirR4Adapter.create_patient`, plus the `OpenEmrAdapter` override if task 1 needs it.
5. The live harness `verify_patient_create.py`, then the release.

**QueueCare server**
0. **Pre-existing bug, fixed first and shippable alone:** commit the kiosk matcher's ref (inside
   its own savepoint and `try`), and wrap the matcher's ref insert in `begin_nested()`
   (Correction 5). Route test reads the ref back through a fresh session.
1. Pin bump to v0.15.0 in both services, together.
2. A read-only count on staging (a script: duplicate authoritative refs, and duplicate open
   identity reports per patient and kind), then migration 083 DDL (§4, never raising) and the
   health fields. Update `ExternalPatientRef.__table_args__` (`models/external_patient_ref.py:12-22`)
   to the partial 076 predicate and declare `uq_ext_patient_authoritative`, and declare the new
   `PatientIdentityReport` index, so autogenerate shows no drift (W2RR N6).
3. `authoritative_ref` and the ref-write rule (§4.3), adopted by `enqueue`, `claim_and_attempt`,
   intake and the matcher; ingest's superseded filter; `reset_roster` superseding refs (DP17).
4. `file_report` dedupe, the new kinds, `link_identity_check`, and the DP5 compare-only ingest.
5. `bookings.identity_unverified` set at insert, the voice path's `allow_hms_write_back`, and the
   write-back gate (§3.3).
6. The confirm-time link (§3.1) and the intake match (§3.2); `MatchOutcome.candidate_ids`; the
   ingest-raising-search test.
7. The `reconciliation_items` service: upsert, auto-close, kiosk queueing (§3.4), DP13.
8. The worklist API: list (PHI-logged), summary, detail with live candidates, search, link,
   dismiss.
9. The Create in HMS endpoint (§5): claim, GUC re-set, search-before-create, outcomes, stale claim.
10. Identity-report resolutions and write-back item actions (§6).
11. RBAC entries, audit events, and the kiosk `hms_link` field.
12. The live OpenEMR round trip (§10).

**QueueCare kiosk**
1. The `hms_link` API types; the token-screen cue; i18n in 8 languages; tests.

**Hospital-portal**
1. `gen:schemas`, and the `access.ts` permissions.
2. The Worklist tab: list, filters, summary strip.
3. The patient-item drawer: search, side-by-side compare, link, create form, dismiss.
4. Identity-report and write-back item actions.
5. The confirm-modal 409 inline resolution.

## 13. Out of scope

- Households: a non-unique phone and choosing among members (B5).
- Stage 3 auto-create. It stays gated on the kiosk recording consent.
- Documents on the worklist (B1).
- Kiosk document decode and QR decoder fixes (DP6).
- The realtime-voice attempt-cap residual (DP3).
- A staff name-edit path for local patients.
- Turning on `HMS_WRITE_BACK_ENABLED`, portal OpenEMR auth options, and CareLoop `decision`
  branching (all SP4).
- Multiple active integrations per hospital.
- Bulk actions of any kind.

## 14. Follow-ups (deferred from the W2RR re-review; pre-existing, not SP3 scope)

- **A fourth ref reader.** `clinical/pdf_password.py:98-107` takes any ref's MRN with `limit(1)`, no
  ordering and no superseded filter, so a PDF password can come from a household member's record or
  an old server's MRN. Decide whether `authoritative_ref` should serve it.
- **DP17 and phone-less patients.** After a vendor or `base_url` change, ingest can't reach a
  phone-less patient through the superseded ref and has no phone to find them by, so it mints a
  second local `Patient` (`ingest_new`) for the same person. Accepted by DP17's intent; recorded.
- **Cross-tenant phone binding.** `patients.phone_hash` is global (B5), so ingest's phone lookup
  (`hms_ingest.py:196-199`) can bind an HMS record to a patient first registered at another tenant.
  A households/B5 matter.
