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

About 300 to 340 HTTP calls, plus up to 30 s waiting for the stale-visit task. Through a
port-forward that is roughly 3 to 8 minutes. The `SUMMARY` line prints the real call
count and elapsed seconds.
