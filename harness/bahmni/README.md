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
