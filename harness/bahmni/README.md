# Bahmni live harness

Drives the real `BahmniAdapter` and `BahmniRecordClient` against a live Bahmni and prints
`CHECK <name> PASS|FAIL <detail>` per check (exit 0 only if every check passes). Writes
synthetic `LT-ADAPTER-` records only; never point it at a Bahmni holding real patients.
Excluded from `pytest` (testpaths is `tests/`). Results are recorded in SpatiaMed's
internal ops records, not in this repository.

| Script | What it does |
|---|---|
| `wait_ready.py [timeout_s]` | Polls `/openmrs/ws/rest/v1/session` until it answers 200 |
| `seed_data.py` | Resolves every uuid the adapter needs by name, creates the synthetic provider, two services, a patient, the marker attribute type and the note-marker concept; writes the config JSON |
| `verify_adapter_live.py` | Runs the checks |

## Env

There are no defaults; each script exits 2 when a required variable is missing.

| Variable | Used by | Meaning |
|---|---|---|
| `BAHMNI_BASE_URL` | all | e.g. `https://localhost:8443` through the port-forward |
| `BAHMNI_USER`, `BAHMNI_PASS` | seed, verify | an OpenMRS user with admin rights on the test instance |
| `BAHMNI_CONFIG_JSON` | seed (writes), verify (reads) | path to the config JSON (`config.json` here is gitignored) |
| `BAHMNI_CA_PEM` | all, optional | PEM bundle that signs the server certificate |
| `BAHMNI_INSECURE_TLS=1` | all, optional | skip TLS verification (a throwaway self-signed test box only) |
| `BAHMNI_LOCATION_NAME` | seed, optional | appointment location name (default `OPD-1`) |
| `BAHMNI_DRUG_QUANTITY_UNITS` | seed, optional | dosing-unit concept name for drug-order `quantityUnits` (default `Unit`) |
| `HARNESS_SAMPLES_DIR` | verify, optional | write raw Bahmni responses there (gitignored `samples/`) |
| `HARNESS_ONLY` | verify, optional | comma-separated check names to run alone |
| `HARNESS_CONTINUE_AFTER_GATE=1` | verify, optional | keep running after the `marker_searchable` setup gate fails |

## Reaching a box with no inbound rules

Forward local 8443 to the Bahmni proxy's port **443** (not 8443) with SSM:

```bash
aws ssm start-session --target "$BOX_INSTANCE_ID" --region "$AWS_REGION" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["443"],"localPortNumber":["8443"]}'
```

The forward can drop after a while; run it in a reconnect loop. Then:

```bash
export BAHMNI_BASE_URL=https://localhost:8443 BAHMNI_CONFIG_JSON=harness/bahmni/config.json
python harness/bahmni/wait_ready.py 900
python harness/bahmni/seed_data.py
HARNESS_SAMPLES_DIR=harness/bahmni/samples python harness/bahmni/verify_adapter_live.py
```

The `marker_searchable` check is a setup gate: if the marker person-attribute type is not
searchable through `bahmni/search/patient`, integration setup must stop and the attribute
must be added to the Bahmni patient-search configuration.

## ADT checks (`--adt`)

> **WARNING: `admitted_visit_survives_stale_close` stops every idle, non-admitted visit at
> a Visit Location on the box, and that cannot be undone.** It belongs to everyone on the
> box, not just this run. It runs only with both `HARNESS_ADT_GLOBAL_STATE=1` and
> `HARNESS_ADT_STALE_CLOSE=1`. Use it only on a throwaway box that nobody else is using.

`verify_adapter_live.py --adt` runs the inpatient group in `adt_checks.py` instead of the
checks above: wards, beds, worklists, dispositions, admit, transfer, discharge, end visit.
Each line is `CHECK <name> PASS|FAIL|INCONCLUSIVE|SKIP <topic> <detail>`:

- `PASS`: the expected behaviour holds. A record-only check passes once it has its value,
  which it prints after `record`.
- `FAIL`: it does not hold.
- `INCONCLUSIVE`: the probe could not decide. This covers an HTTP 403 (a real privilege
  finding for the ADT login; the line names the privilege when Bahmni reports one) and a
  check blocked because an earlier one did not complete.
- `SKIP`: a box-wide probe that was not enabled.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | every check PASSed or SKIPped |
| 1 | a FAIL or INCONCLUSIVE, or an early stop (gate, deadline, dead tunnel) |
| 3 | a box-wide change could not be restored (see below) |
| 4 | cleanup incomplete (`LEFT` lines name what to remove by hand) |
| 130 | interrupted |

CHECK and CLEANUP lines carry counts, booleans, stock names, status codes and exception
class names. They never carry uuids, server messages or response bodies, so they can be
pasted into a PR. Server messages and bodies go to `HARNESS_SAMPLES_DIR`.

### Logins

- The checks run as `BAHMNI_USER`, which is an API login with the ADT and bed
  privileges. With bahmni-ipd (1.1.x) installed, an admission also needs `Add Tasks` and
  `Edit Tasks`: the IPD module creates nursing tasks when an ADMISSION is saved, and
  without them admit is refused with HTTP 403.
- Cleanup and the three box-wide probes run as the OpenMRS admin, `BAHMNI_ADMIN_USER` /
  `BAHMNI_ADMIN_PASS`. The admin login is read once and never printed. Without it, cleanup
  uses `BAHMNI_USER` and the box-wide probes SKIP.

### Run it inside tmux

Run it inside `tmux`, on the machine that holds the port-forward. SIGINT, SIGHUP and
SIGTERM stop the run the way Ctrl-C does: the probe in progress restores what it changed,
then cleanup runs. Signals that arrive during cleanup are ignored.

```bash
tmux new -s adt
export BAHMNI_BASE_URL=https://localhost:8443 BAHMNI_CONFIG_JSON=harness/bahmni/config.json
export BAHMNI_CA_PEM=/path/to/ca.pem          # or BAHMNI_INSECURE_TLS=1 on a throwaway box
read -rs BAHMNI_PASS; read -rs BAHMNI_ADMIN_PASS; export BAHMNI_PASS BAHMNI_ADMIN_PASS
export BAHMNI_USER=<api login> BAHMNI_ADMIN_USER=<admin login>
export HARNESS_SAMPLES_DIR=harness/bahmni/samples
python harness/bahmni/seed_data.py --adt
python harness/bahmni/verify_adapter_live.py --adt
# box-wide probes as well (a throwaway box only):
HARNESS_ADT_GLOBAL_STATE=1 HARNESS_ADT_STALE_CLOSE=1 python harness/bahmni/verify_adapter_live.py --adt
```

`seed_data.py --adt` resolves the ADT keys by name and creates nothing. Provision the
tenant first: the IPD visit type, the ADMISSION/TRANSFER/DISCHARGE types, a "Discharge
Summary" type, the emr GPs and the disposition mappings.

| Variable | Meaning |
|---|---|
| `BAHMNI_ADMIN_USER`, `BAHMNI_ADMIN_PASS` | verify: the admin login for cleanup and the box-wide probes |
| `BAHMNI_VISIT_LOCATION_NAME` | seed, optional: the Visit Location by name. The default is the nearest "Visit Location"-tagged ancestor of the login location |
| `HARNESS_ADT_GLOBAL_STATE=1` | verify: also run the box-wide probes (needs the admin login and `HARNESS_SAMPLES_DIR`) |
| `HARNESS_ADT_STALE_CLOSE=1` | verify: also run the irreversible stale-visit close |
| `HARNESS_ADT_STALE_TASK_TIMEOUT_S` | verify: read timeout of the one call that runs the stale-visit task (default 600). The call returns only after every idle visit at a Visit Location is closed, which outlasts the usual 30 s on a box with many open visits |
| `HARNESS_DEADLINE_S` | verify: no new check starts after this many seconds (default 1200). It is checked between checks, so one slow check can overrun it. Cleanup still runs |
| `HARNESS_ONLY`, `HARNESS_CONTINUE_AFTER_GATE` | as above. `module_versions` and `adt_config_resolved` are gates |

### Shared-box safety

- Every record it writes is synthetic and named `LT-P2-<RUN>`. RUN is six random letters,
  printed on the first line.
- It never touches the seed patient or anything it did not create. A bed type it
  did not create is never deleted (`CLEANUP bed type … SKIPPED`).
- Order: read-only checks, then this run's master data (a 2x3 ward with four beds, a child
  room, a bed type, plain locations), then this run's patients, and last the box-wide
  probes:
  - `worklist_misconfig` blanks `emrapi.sqlSearch.patientsToAdmit`;
  - `ward_admission_tag_missing` renames the "Admission Location" tag, so every ward
    operation on the box fails for those seconds;
  - `admitted_visit_survives_stale_close` (see the warning above).
- Three transport errors in a row stop the checks, as does the deadline. Cleanup runs
  either way.
- A signal during a restore lets that restore finish, then stops the run: no further
  check or box-wide probe starts.
- Cleanup stops after three items in a row fail on transport errors. It prints
  `CLEANUP ABORTED` and one `LEFT <kind> <name>: <what to do>` line per item left, then
  exits with code 4.

### Restore procedure

1. Before changing anything, each box-wide probe writes the original value to
   `$HARNESS_SAMPLES_DIR/RESTORE-<RUN>.json` (mode 0600; `samples/` is gitignored) and
   prints `BOXSTATE changed …`.
2. It restores the value in its own `finally`, checks it with a read-back, and tries twice.
3. If the restore still fails, the run prints `RESTORE FAILED <what>: manual: POST …` with
   the exact call and the restore file. Then `!!! BOX STATE NOT RESTORED` follows the
   SUMMARY, the exit code is 3, and no further box-wide probe starts.
4. First check by hand whether the value is already back: the read-back route
   (`GET locationtag/<uuid>`) is not yet verified live. To put the value back, use the
   admin login and the `original` value from the restore file:
   - a GP: `POST /openmrs/ws/rest/v1/systemsetting/<uuid> {"value": <original>}`;
   - the tag: `POST /openmrs/ws/rest/v1/locationtag/<uuid> {"name": "Admission Location"}`.
5. `RESTORE FAILED` lines name uuids. Do not paste them into a PR.

### Cleanup

A final `finally` undoes everything this run created, with one `CLEANUP <kind> <name>
OK|SKIPPED|FAILED` line each:

1. beds unassigned, visits ended, patients voided;
2. beds deleted;
3. rooms, then wards and locations retired;
4. this run's bed type deleted.

OpenMRS voids and retires rather than deletes, so the per-run name is what keeps reruns
apart. A `kill -9` or a dead tunnel leaves records behind. Find them by the printed
`LT-P2-<RUN>` name.

### Runtime

About 300 to 340 HTTP calls, plus the stale-visit task run (up to `HARNESS_ADT_STALE_TASK_TIMEOUT_S`)
and up to 30 s waiting for it to close the control visit. Through a
port-forward that is roughly 3 to 8 minutes. The `SUMMARY` line prints the real call
count and elapsed seconds.

## Orders and lab checks (`--orders`, `--lab`)

`verify_adapter_live.py --orders` and `--lab` run the orders and lab groups in `phase3_checks.py`
instead of the default checks. Lines and statuses are as for `--adt`, plus `INFO` for an
optional check that was not configured. Exit codes: 0 nothing FAILed or was
INCONCLUSIVE; 1 otherwise or an early stop; 2 bad env (missing variables, or the `lab`
extra is not installed); 4 cleanup incomplete; 130 interrupted.

> **WARNING: `--lab` records sample collections in the lab system, and they cannot be
> undone.** The lab DB role is read only, so this run's lab samples stay behind (they belong
> to voided `LT-P3-<RUN>` patients). `added_test_not_cancelled` makes the lab system cancel
> a synthetic test on purpose, and `duplicate_accession` reuses an accession number. Run it
> only on a box that holds synthetic data.

| Group | Check | Topic | What it proves |
|---|---|---|---|
| `--orders` | `visit_orders_read` | `visit_order_read` | a non-coded drug order and a lab order read back from the visit; `drugNonCoded` on the full rep; concept null |
| `--orders` | `lab_tests_catalogue` | `lab_test_catalogue` | the orderable set lists tests (counts only) |
| `--orders` | `fulfiller_details_write` | `fulfiller_status_mirror` | `fulfillerdetails` writes COMPLETED, reads back, and a repeat writes nothing |
| `--orders` | `medicationdispense_absent` | `fulfiller_status_mirror` | `GET medicationdispense` answers 404 |
| `--orders` | `noncoded_drug_not_in_odoo` | `noncoded_drug_no_sale_line` | 60 s after the write, a drug-only synthetic patient has no billing-system sale line (found by partner `ref` = patient identifier or by name, not by line name; product names go to samples; INFO without `ODOO_*`) |
| `--lab` | `elis_login_path` | `lab_login_path` | the form login yields a session |
| `--lab` | `lab_order_to_sample` | `lab_order_sample` | one uncollected sample per sample type within 30 s |
| `--lab` | `guard_header_required` | `lab_write_guard` | without the guard header, GET, HEAD and case-varied or encoded `provider` names get 403; with it, the read provider answers (INCONCLUSIVE, with the header-less statuses, when no sample exists for that read) |
| `--lab` | `collect_verified` | `sample_collect` | the client's collect: 204, accession and date in the lab DB, a new event for this sample |
| `--lab` | `accession_len` | `sample_collect` | our accession fits 13 characters (note the analyser limit by hand) |
| `--lab` | `event_record_columns` | `sample_collect` | which `event_records` column names the sample's encounter |
| `--lab` | `sibling_orders_marked` | `sample_collect` | whether collecting one sample type marks both orders in the record |
| `--lab` | `full_list_collect_cancels_nothing` | `collect_keeps_orders` | 30 s after the collect, every lab order is still active and no test is gone |
| `--lab` | `added_test_not_cancelled` | `collect_keeps_orders` | a test added between the client's read and its send makes the client raise `lab_tests_changed` |
| `--lab` | `duplicate_accession` | `duplicate_accession` | whether the lab system refuses an accession number already in use on the sibling sample, else the added-test sample while still uncollected (record only) |

The harness never builds the collect call: every collect goes through
`OpenElisClient.collect`, which sends the full test list it read just before. The guard
probe sends its header-less requests only to the read provider with `sampleId=0`.

```bash
pip install -e ".[dev,lab]"
python harness/bahmni/seed_data.py --phase3
python harness/bahmni/verify_adapter_live.py --orders
# forward the lab DB port as well as 443 for --lab
read -rs ELIS_PASS; read -rs ELIS_GUARD_KEY; read -rs ELIS_RO_DSN
export ELIS_PASS ELIS_GUARD_KEY ELIS_RO_DSN
export ELIS_BASE_URL=https://localhost:8443 ELIS_USER=<lab login> ELIS_ACCESSION_PREFIX=SM
python harness/bahmni/verify_adapter_live.py --lab
```

| Variable | Used by | Meaning |
|---|---|---|
| `BAHMNI_LAB_ORDERABLE_SET` | seed `--phase3`, optional | the orderable lab concept set by name (default `Laboratory orders`) |
| `ELIS_BASE_URL`, `ELIS_USER`, `ELIS_PASS` | `--lab` | the lab system through the port-forward, and its login |
| `ELIS_GUARD_KEY` | `--lab` | the proxy guard header value |
| `ELIS_RO_DSN` | `--lab` | `postgresql://user:password@host:port/db` for the read-only lab DB role |
| `ELIS_ACCESSION_PREFIX` | `--lab` | 1 to 3 uppercase letters; the run issues `<prefix><yymmdd>9nnn` |
| `ELIS_CA_PEM`, `ELIS_INSECURE_TLS=1` | `--lab`, optional | as the `BAHMNI_` pair |
| `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASS` | `--orders`, optional | all four set: `noncoded_drug_not_in_odoo` reads sale lines over XML-RPC |
