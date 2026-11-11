# EDAI-1 DP1–DP3 pipeline runbook

Audience: the project owner, operating the 5GB rehearsal from a terminal.

EDAI-1 runs as **three independent Airflow DAGs**. Each DagRun is one pipeline and one
Spark profile, and the DAGs hand over through immutable manifests on GCS:

| DAG | Pipeline | Reads | Publishes after physical **and** semantic validation |
|---|---|---|---|
| `dp1_source_to_bronze` | Source → Bronze | the immutable Source manifest | `dp1_handoff.json` |
| `dp2_bronze_to_curated` | Bronze → Silver → Core Gold | a verified `dp1_handoff.json` | `dp2_handoff.json` |
| `dp3_historical_features` | Silver + Core Gold → historical features | a verified `dp2_handoff.json` | `dp3_completion.json`, `campaign_manifest.json` |

**Why three DAGs:** each stage can be triggered, observed, recovered and evidenced on its own,
and a downstream DAG cannot start paid work until its upstream DAG has proved success in a
durable manifest, not in the Airflow UI. There is no winner selection and no promotion:
Baseline and Optimized are separate chains that read the same Source and never each
other's intermediates. A complete A/B campaign is six DagRuns.

The integrated DAG `edai1_data_pipeline_campaign` (all stages in one DagRun) is retired. Its
source is kept in [`archive/legacy-orchestration/`](../../../../archive/legacy-orchestration/README.md);
its interrupted DagRun (benchmark `ab-20261111033139`, abandoned) stays in the Airflow
metadata untouched.

**Nothing in the three-DAG workflow has run on Dataproc yet.**

## Part 0 — Provision and publish (owner action)

`rehearsal-5gb-v1` has one registered bucket, `ewallet-data-ai-platform-hg26-rehearsal-5gb-data`
(`data_platform/config/dataset_identity.py`), used for its Source, Delta runs, reports and
event logs. The temporary bucket stays shared. The canonical bucket and the Terraform
outputs are not changed; the submitters switch bucket from the registry.

**Who needs access:**

| Identity | Needs | How |
|---|---|---|
| Spark `ewallet-spark@…` | Delta writes, reports, event logs, Source read | `roles/storage.objectUser` on this bucket (the tool grants it; matches the canonical bucket) |
| Owner / Airflow (`gcloud` ADC) | Source publication and readback, read-only checks | Project owner already has it through the default legacy bindings |

1. Review the exact commands, then create the bucket (creates only this bucket, safe defaults,
   skips what exists, then verifies):
   ```bash
   python -m ops.gcp.rehearsal_storage plan
   python -m ops.gcp.rehearsal_storage apply --execute \
     --confirm-bucket ewallet-data-ai-platform-hg26-rehearsal-5gb-data
   python -m ops.gcp.rehearsal_storage check   # read-only; exits 0 when ready
   ```
   These read `terraform output`. If Terraform is not initialized in this checkout, pass
   `--project-id ewallet-data-ai-platform-hg26 --spark-service-account ewallet-spark@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com`
   or `--terraform-output-json <file>`.
2. Publish. Without `--execute` the same command is a dry run. Generated, uploaded and
   verified one shard at a time per worker; its local staging is then removed:
   ```bash
   GCP_PROJECT_ID=ewallet-data-ai-platform-hg26 DATAHUB_GMS_URL=http://datahub.invalid \
   uv run --locked --project data_platform/generation python -m ops.gcp.generate_source_dataset \
     --config data_platform/generation/config/settings_merchant_history.yaml \
     --environment gcp --dataset-version rehearsal-5gb-v1 --global-seed 7001 --shard-count 186 \
     --users-per-shard 12500 --merchants-per-shard 300 --transactions-per-shard 100000 \
     --workers 2 --staging-dir /path/with/free/space --execute
   ```
   Expected plan digest: `422c15a169165158fcd7d432a7943d50812b5f3e55e7d0c6aec7c457371c6c83`.
   With `--execute` the command first checks the bucket through the Terraform outputs. If this
   checkout has no initialized Terraform, save them from one that does
   (`terraform -chdir=infra/terraform/gcp/environments/dev output -json > /abs/outputs.json`) and add
   `--terraform-output-json /abs/outputs.json`. The DAG's `terraform_output_json` takes the same file.
   Re-running after an interruption resumes: verified shards are skipped and conflicting
   objects stop the publication. Allow about 1.5 hours with 2 workers.

**Status:** the bucket was provisioned and `rehearsal-5gb-v1` was published and verified on 2026-11-10
(manifest SHA-256 `ac2dfb0eac16e173261b3c3f65e83a03d388528538daefd9ec669bef7123eb80`). Do not publish it again.

**Fresh checkouts:** the Terraform outputs file is not in the repository. Produce one where Terraform is
initialized, as shown above, keep it out of Git, and pass its absolute path as `terraform_output_json`.

**Broken IPv6:** on a network that black-holes IPv6, each `gcloud` connection can stall about 12 seconds
before falling back to IPv4 (check with `curl -6 -m 20 https://storage.googleapis.com/`). Airflow tasks
make few calls, so this only slows them and no workaround is required. For bulk GCS work such as a
re-publication, prefer IPv4 for that process only: put a `sitecustomize.py` that wraps
`socket.getaddrinfo` to map `AF_UNSPEC` to `AF_INET` in a private directory, and set
`PYTHONPATH=<that directory> CLOUDSDK_PYTHON_SITEPACKAGES=1` on the command. Do not change system networking.

## Part 1 — Verify the Source dataset

```bash
python -m ops.gcp.verify_source_dataset --bucket ewallet-data-ai-platform-hg26-rehearsal-5gb-data \
  --dataset-version rehearsal-5gb-v1 --output source-gate.json
```
It exits non-zero on any violation. It checks the manifest and plan, the registry pins
(186 shards, seed 7001, generator config, 4.5–5.5 GB), the merchant_history extension
and every object's size and MD5. Keep `manifest_sha256` from the report: every DagRun
needs it as `source_manifest_sha256`.

## Part 2 — Data Generator evidence

- **Volume, duplicate rate, V1/V2 schema evolution:** existing tool, from the
  control files:
  ```bash
  python -m data_platform.generation.src.sharded.profile_manifest --plan plan.json \
    --manifest dataset_manifest.json --output profile.json
  ```
- **Merchant skew and cardinality:** that tool reports them as `PENDING_COLUMN_SCAN` on purpose. The exact
  read-only scan of `rehearsal-5gb-v1` is:
  ```bash
  uv run --locked --project data_platform/generation python -m ops.gcp.profile_rehearsal_5gb \
    --output tmp/edai1-5gb-evidence/column_profile.json
  ```
  It needs Application Default Credentials and several GiB of RAM, and it is limited to this dataset. It is
  not a 100 GB profiler; use Trino for a column scan there.
- **Merchant history:** `merchant_history` rows in the manifest (`tables`), and in Trino
  `SELECT merchant_id, count(*)` on Silver `merchant_history`.

## Part 3 — Architecture

### Task graphs

Every task runs `python -m ops.gcp.campaign_task <action>` (shared runner) with its
parameters in the environment; the graphs come from
[`edai1_pipeline.py`](../../../../data_platform/orchestration/airflow/edai1_pipeline.py). All
three DAGs are `schedule=None`, one active run, one task at a time, and every task needs the
exact approval token.

```text
dp1_source_to_bronze
  preflight.verify_identity_and_absence -> preflight.verify_source_dataset
  -> bronze.wait_quota -> bronze.submit_transformation -> bronze.physical_validation
  -> bronze.semantic_validation -> finalize.publish_dp1_handoff

dp2_bronze_to_curated
  preflight.verify_identity_and_absence -> preflight.verify_dp1_handoff -> preflight.verify_bronze_inputs
  -> silver.wait_quota -> silver.submit_transformation -> silver.physical_validation -> silver.semantic_validation
  -> gold.wait_quota -> gold.submit_transformation -> gold.physical_validation -> gold.semantic_validation
  -> finalize.publish_dp2_handoff

dp3_historical_features
  preflight.verify_identity_and_absence -> preflight.verify_dp2_handoff -> preflight.verify_silver_gold_inputs
  -> features.wait_quota -> features.submit_transformation -> features.physical_validation
  -> features.semantic_validation -> finalize.publish_dp3_completion
```

| Task | Checks | Paid | Retries |
|---|---|---|---|
| `verify_identity_and_absence` | clean checkout at `git_sha`, Terraform deployment = DagRun, bucket isolation, **this pipeline's** output prefixes, batch IDs and manifest absent | no | 0 |
| `verify_source_dataset` (DP1) | Source manifest SHA-256, plan, registry pins, every object | no | 2 |
| `verify_dp1_handoff` / `verify_dp2_handoff` | the upstream manifest (and its own upstream chain), identity, run IDs, batch states, and the upstream validation reports re-read | no | 2 |
| `verify_*_inputs` | exact Delta table inventory of the upstream runs | no | 2 |
| `wait_quota` | Dataproc CPU/disk quota for the initial allocation (28 vCPU) | no | 0 |
| `submit_transformation` | the existing submitter; refuses an existing output prefix | **yes** | 0 |
| `physical_validation` | exact Delta table inventory and `_delta_log` | no | 2 |
| `semantic_validation` | the existing validator on Dataproc; requires a PASS report for this run | **yes** | 0 |
| `publish_*` | collects provenance again, requires every batch `SUCCEEDED` and every report PASS, then writes the manifest once | no | 2 |

DP2 and DP3 check the upstream handoff again immediately before each paid submission, and Gold
is refused unless Silver's validation report of the same DagRun is PASS.

### Handoff contract

All manifests are under `gs://<bucket>/lakehouse/campaigns/<benchmark_id>/<profile>/`:

| Object | Written by | Content |
|---|---|---|
| `dp1_handoff.json` | DP1 `finalize` | schema `edai1-pipeline-handoff-v1`, `status: SUCCESS`, benchmark, dataset, profile, Git SHA, Source SHA, bucket, project, region, producing DAG and DagRun, Bronze run ID, output root, Dataproc batch, Spark application, event log, Spark UI endpoint, validation report and status, Source summary |
| `dp2_handoff.json` | DP2 `finalize` | the same identity, Silver and Gold records, and `upstream`: the DP1 handoff URI, its SHA-256 and DagRun |
| `dp3_completion.json` | DP3 `finalize` | the same identity, the features record, `upstream` = the DP2 handoff, and the campaign manifest URI and SHA-256 |
| `campaign_manifest.json` | DP3 `finalize` | schema `edai1-pipeline-campaign-v2`, all four stages, the Source re-verified: the input of `compare_campaign_outputs.py` |

Rules: a manifest is published only after every stage of its pipeline passed both validations;
it is written once (`--if-generation-match=0`); a retry may only find identical content (apart
from the timestamp) and any other existing content is a conflict that fails the task. A consumer
refuses a missing, unreadable or inconsistent manifest, a different benchmark, dataset, Source
SHA, Git SHA, profile, bucket or run ID, a non-PASS validation or non-`SUCCEEDED` batch, an
upstream report that no longer passes, and a DP2 handoff whose recorded DP1 SHA-256 differs from
the DP1 handoff now on GCS. There is no fallback to the canonical 100GB outputs.

### Approval

Each DagRun needs `execution_approval` =
`EXECUTE-PAID-DATAPROC:<DP1|DP2|DP3>:<benchmark_id>:<profile>:<dataset_version>:<first 12 of git_sha>`.
A token for one pipeline or profile is refused by every other one. The scripts below compute it
and add it only for `--execute --confirm-paid`; prepared files always hold `NOT_APPROVED`.

### Spark configuration (unchanged)

[`spark_profiles.py`](../../../../data_platform/config/spark_profiles.py) is not changed by this
workflow. Resources for every stage and profile: driver/executor 4 cores, 9600m memory, 3840m
overhead, dynamic allocation 2/6/7.

| Stage | Baseline | Optimized |
|---|---|---|
| DP1 Bronze | AQE off, 200 partitions | AQE on, 200, allocation profile `performance` |
| DP2 Silver | AQE off, 200 | AQE on, 200, coalescing on, `parallelismFirst=false`, advisory 64 MiB |
| DP2 Gold | AQE off, 200 | AQE on, 400 |
| DP3 Features | AQE off, 200 | AQE on, 200, coalescing on, `parallelismFirst=false`, advisory 64 MiB |

Skew join is off everywhere. `run_dpN.sh` prints the resolved values for its stages.

## Part 4 — Execute the six DagRuns

**Before any paid run, free the Dataproc quota.** The first gate needs 28 vCPU of the
`CPUS_ALL_REGIONS` quota (32). During the abandoned 2026-11-11 attempt, `dp1_bronze.wait_quota`
saw 20 vCPU in use and only 12 free for nine minutes, consistent with the 5-node GKE lab being
awake. Check with `python3 ops/gcp/cloud_lab.py status` and put the lab to sleep
(`plan-sleep`, then `sleep` with its approval) before DP1.

All commands run from the primary checkout on `main`; the benchmark is pinned to the Git SHA at
preparation, so do not commit to `main` until all six DagRuns have finished (a later DagRun of a
moved checkout refuses to start).

```bash
# 1. Airflow (API server, scheduler, DAG processor on 127.0.0.1:8084)
./scripts/start_airflow_local.sh

# 2. Plan the benchmark: new benchmark ID, 8 run IDs, 6 DagRun configurations, Source verified (read-only)
bash scripts/edai1/prepare_campaign.sh \
  --terraform-output-json /abs/path/terraform-outputs.json   # or omit to run `terraform output -json`
B=ab-YYYYMMDDhhmmss   # printed by prepare

# 3. DP1 Baseline: dry run (read-only checks), then execute
bash scripts/edai1/run_dp1.sh --benchmark-id "$B" --profile baseline
bash scripts/edai1/run_dp1.sh --benchmark-id "$B" --profile baseline --execute --confirm-paid
bash scripts/edai1/status_campaign.sh --benchmark-id "$B"          # repeat while it runs

# 4. Verify the DP1 handoff
bash scripts/edai1/verify_handoffs.sh --benchmark-id "$B" --profile baseline

# 5. DP2 Baseline (refuses without a verified DP1 handoff)
bash scripts/edai1/run_dp2.sh --benchmark-id "$B" --profile baseline
bash scripts/edai1/run_dp2.sh --benchmark-id "$B" --profile baseline --execute --confirm-paid

# 6. Verify the DP2 handoff
bash scripts/edai1/verify_handoffs.sh --benchmark-id "$B" --profile baseline

# 7. DP3 Baseline (refuses without a verified DP2 handoff)
bash scripts/edai1/run_dp3.sh --benchmark-id "$B" --profile baseline
bash scripts/edai1/run_dp3.sh --benchmark-id "$B" --profile baseline --execute --confirm-paid
bash scripts/edai1/verify_handoffs.sh --benchmark-id "$B" --profile baseline

# 8. Repeat 3-7 with --profile optimized

# 9. Final state of all six DagRuns, with batch IDs, Spark application IDs and Spark UI links
bash scripts/edai1/status_campaign.sh --benchmark-id "$B" --dataproc
```

Script reference (all have `--help`; exit 0 = ready/done, 1 = refused or not ready, 2 = usage):

| Script | Purpose | Changes |
|---|---|---|
| `prepare_campaign.sh` | Requires `main` and a clean tree; pins Git SHA, Source SHA (default: the published `ac2dfb0e…`) and the Terraform outputs (copied); writes `tmp/edai1-campaign/<B>/campaign.json` and `{baseline,optimized}/{dp1,dp2,dp3}.json` (mode 600, Git-ignored) | local files only |
| `run_dp1.sh`, `run_dp2.sh`, `run_dp3.sh` | Dry run: Git at the pinned SHA, DAG registered, DagRun ID unused and no active run, upstream handoff verified, own output prefix, batch ID and handoff absent (read-only `gcloud`); `--offline` skips the `gcloud` reads | with `--execute --confirm-paid` only: `airflow dags unpause` and `airflow dags trigger` of that one DAG |
| `status_campaign.sh` | Airflow state and current task of the six planned DagRuns, handoff state, and from each published handoff the batch, Spark application, Spark UI and event log; `UNKNOWN`/`NOT_AVAILABLE` when a value cannot be read | none |
| `verify_handoffs.sh` | DP1 → DP2 → DP3 for one profile, with the same checks as the DAGs | none |

DagRun IDs are `<B>-<profile>-dp1|dp2|dp3`; the Airflow link is
`http://127.0.0.1:8084/dags/<dag_id>/runs/<dagrun id>`.

### Semantic comparison (separate approval)

After both `dp3_completion.json` exist. The comparator needs distributed Spark with Delta and the
GCS connector, so it runs as one Dataproc batch in the campaign environment (runtime 2.2, pinned
Delta JARs, repository archive, common resource contract, no Baseline/Optimized SQL profile). It
reads the two `campaign_manifest.json` files and the outputs they name, and writes only
`campaigns/<B>/comparison.json`. Without `--execute` it prints the command and calls nothing:
```bash
B=ab-YYYYMMDDhhmmss; SHA=<the benchmark's pinned Git SHA>
python -m ops.gcp.submit_campaign_comparison --benchmark-id $B --dataset-version rehearsal-5gb-v1 \
  --git-sha $SHA --terraform-output-json tmp/edai1-campaign/$B/terraform-outputs.json   # dry run
# paid, after approval (batch c1-<B without ab->, TTL 2h, at most 8 four-core processes):
python -m ops.gcp.submit_campaign_comparison ... --execute
```
`--execute` first re-checks, read-only, that both manifests are comparable and pinned to `$SHA`,
that the comparison code equals that commit, and that the batch and report do not exist. GKE
must be asleep (the batch starts with 28 vCPU). If a batch fails before writing the report,
retry with `--attempt 2`; a written report is final. The verdict is `SEMANTIC_EQUIVALENT`, `SEMANTIC_MISMATCH` or `COMPARISON_INCOMPLETE`. Only
`created` (five feature tables) and `created_timestamp` (`feat_user_90d`) are excluded; each
run's validators check those against its own recorded clock. Performance comes from
`python -m ops.gcp.spark_stage_report` on the event logs.

## Part 5 — Evidence

Capture only what shows the rubric; the manifests already hold the identifiers.

| Screenshot | Where |
|---|---|
| DP1, DP2, DP3 Graph views, all green | Airflow → DAG → the DagRun → Graph (DP2 shows the `silver` and `gold` groups) |
| One semantic validation log per DAG | the `semantic_validation` task log: `PASS: validation <scope> batch=… report=…` |
| Six completed DagRuns | Airflow → DAG runs list, or `status_campaign.sh` |
| Spark UI of DP2 Silver, DP2 Gold, DP3 Features (both profiles) | link from `status_campaign.sh` (`spark_ui=`) or the Dataproc batch page; jobs/stages, SQL plan with `AQEShuffleRead` for Optimized, stage task-time and spill |

Navigation from a DAG to its data:
```text
Airflow DAG run -> task log of submit_transformation (Dataproc batch ID = the run ID)
  -> Dataproc batch (gcloud dataproc batches describe <run id> --region asia-southeast1)
  -> Spark UI (batch page) or event log gs://<bucket>/spark-events/<application id>
  -> Delta output gs://<bucket>/lakehouse/runs/<run>/<zone>/
  -> validation report gs://<bucket>/lakehouse/runs/<run>/evidence/validation-<scope>-a<n>.json (PASS)
```
Validator batches are named `v<attempt>-<run ID without "ab-">` (at most 50 characters).
Every transformation and validator writes `runs/<run>/evidence/spark_execution-<batch>.json` at
startup (Spark application ID, event-log directory); the runner keeps
`execution_provenance-<batch>.json` beside it, also for failed batches. The Serverless Spark UI
may not stay available after a batch ends, so capture it during or right after the run, or from
the event logs.

### Historical evidence

The 100GB results under `docs/evidence/` were produced by the earlier DAG designs (Method-2
single DAG, then the integrated campaign) and stay as they are. The three-DAG workflow has not
yet produced evidence; do not present the historical runs as three-DAG runs.

## Part 6 — Recovery

Output prefixes and manifests are immutable, so nothing is ever re-run over its own output.

- **Failed before any paid job** (preflight, handoff check, quota): fix the cause and clear the
  failed task. Nothing was created.
- **Failed validator:** read its report. If the data is at fault, the pipeline stops there and no
  handoff is published. If the batch failed for an infrastructure reason, clear the
  `semantic_validation` task: the Airflow try number gives a new batch and report (`v2-…`, `-a2`).
- **Failed publication:** it retries twice automatically; clearing it again is safe (write-once,
  identical content).
- **Failed or partial transformation:** clearing the task is refused because the output prefix
  exists. Prepare a **new benchmark** and start again from DP1, so every stage of both profiles
  shares one benchmark. Keep the failed outputs and their provenance; do not delete them as
  routine recovery.
- **Source mismatch:** stop and investigate. Do not publish over the dataset.
