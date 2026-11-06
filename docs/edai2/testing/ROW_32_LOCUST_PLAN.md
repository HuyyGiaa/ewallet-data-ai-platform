# Row 32: Locust against real GKE backends

The owner decided that final row 32 evidence must use deployed backends. This page lists what exists, what is missing and how to run it. Nothing here has run, and the B1 branch is unchanged.

```text
Locust → Fraud API on GKE → Feast SDK → Redis → feature assembly → KServe → prediction
```

## Current capability

On `feature/b1-testing-quality` at `281db72`:

| Part | State |
|---|---|
| `tests/load/locustfile.py` | Real scenarios; `integration` profile exists |
| Remote guard | Needs `EWALLET_LOAD_PROFILE=integration` and `EWALLET_LOAD_ACKNOWLEDGE_REMOTE=I_OWN_THIS_TARGET` |
| Reports | HTML and CSV through `scripts/testing/run_locust.sh`, limits 200 users and 900 s |
| Mock results | Kept as an isolated application benchmark, never as production throughput |

## Missing

**One real gap:** `tests/load/payloads.py` generates `synthetic-user-*` identifiers. No such entity exists in Redis, so a real backend answers `424 missing_feature`, and the run would measure rejections.

| Item | Where | State |
|---|---|---|
| Real entity fixture | `ops/experiments/fraud_entities.py` | Done on the C-C branch |
| Load `EWALLET_LOAD_ENTITIES` in `valid_payload` | `tests/load/payloads.py`, about 15 lines | **Needs owner approval**: it changes B1 and belongs in final integration |
| Separate fault scenarios | Existing `INVALID` and `DEPENDENCY_FAILURE` | Keep out of the valid-request run |

The change draws each request from the file, keeps a new `transaction_id` per request, and falls back to the synthetic generator when the variable is unset. The B1 local run stays the same.

## Required resources

| Resource | Need |
|---|---|
| Nodes | 1 node: `plan-wake --mode minimal --profile kserve-small` computes 1.8 CPU and 3.6 GiB |
| Workloads | `fraud-api`, `fraud-detector-predictor`, `redis`, `kserve-controller-manager` |
| Access | `kubectl port-forward` to `svc/fraud-api`; no new public endpoint |
| Cost | About $0.285/hour, so about $0.86 for 3 hours, including sleep costs |

A port-forward passes through one tunnel, so very high rates would measure the tunnel. An in-cluster Job is the fallback if the tunnel saturates.

## Minimum experiment

| Setting | Value |
|---|---|
| Traffic | Valid requests only: `EWALLET_LOAD_MIX=valid=100` |
| Levels | 5, 10, then 20 users |
| Duration | 5 minutes per level after 1 minute of warm-up |
| Entities | At least 300 verified entities |
| Failures | Every failed request stays in the report |

## Owner steps

```bash
# 1. Wake one node, as approved (Cloud Lab runbook).
# 2. Build and verify real entities.
python -m ops.experiments.fraud_entities build --snapshot "$SNAPSHOT" --limit 500 --out entities.jsonl
kubectl -n ml-platform port-forward svc/fraud-api 18080:80 &
python -m ops.experiments.fraud_entities verify --entities entities.jsonl \
  --url http://127.0.0.1:18080/v1/fraud/predict --out entities-verified.jsonl
# 3. Run each level.
EWALLET_LOAD_PROFILE=integration EWALLET_LOAD_ACKNOWLEDGE_REMOTE=I_OWN_THIS_TARGET \
EWALLET_LOAD_MIX=valid=100 EWALLET_LOAD_ENTITIES=entities-verified.jsonl \
LOCUST_HOST=http://127.0.0.1:18080 LOCUST_USERS=5 LOCUST_RUN_TIME=5m \
scripts/testing/run_locust.sh docs/evidence/edai2/testing/locust-gke-5
# 4. Return to sleep.
```

`verify` keeps the rejection counts. Report them: the snapshot and the online store cover different windows, so some entities may not be served.

## Evidence

- `verify` output with its status counts.
- Locust HTML and CSV for each level.
- Grafana or `/metrics` for the same window.
- Pod CPU and memory, image digests, source SHA and node type.
- Screenshots of the Locust report and the Fraud API metrics.
- A failure analysis, including 424 and 504 responses.
