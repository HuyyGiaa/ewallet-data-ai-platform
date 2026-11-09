# EDAI-1 DP1–DP3 campaign runbook

Audience: the project owner, operating the 5GB rehearsal.

The workflow is the DAG `edai1_data_pipeline_campaign`. One DagRun carries one Spark
profile from the immutable Source dataset through Bronze, Silver, Core Gold and DP3
features. Every stage is followed by a physical Delta check and a semantic validator.
There is no winner selection and no promotion. Baseline and Optimized are two
separate DagRuns that read the same Source.

Nothing in this runbook has been run on Dataproc yet.

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
- **Merchant skew and cardinality:** that tool reports them as `PENDING_COLUMN_SCAN`
  on purpose. They need a column scan; run it in Trino on the Bronze or Silver run
  (`GROUP BY merchant_id`, `count(DISTINCT ...)`).
- **Merchant history:** `merchant_history` rows in the manifest (`tables`), and in Trino
  `SELECT merchant_id, count(*)` on Silver `merchant_history`.

## Part 3 — Baseline

Prerequisites: Airflow runs from a clean checkout of the campaign commit, its Python
has the project on `PYTHONPATH`, `gcloud` is authenticated, and the Dataproc quota is
free. Loading this DAG into Airflow is an owner action.

Trigger `edai1_data_pipeline_campaign` with:

| Parameter | Value |
|---|---|
| `dataset_version` | `rehearsal-5gb-v1` |
| `benchmark_id` | `ab-YYYYMMDDhhmmss`, new for this comparison |
| `spark_profile` | `baseline` |
| `git_sha` | full HEAD of the Airflow checkout (the tree must be clean) |
| `source_manifest_sha256` | from Part 1 |
| `bronze_run_id` | `<benchmark_id>-baseline-bronze-rYYMMDDhhmmss` |
| `silver_run_id` | `<benchmark_id>-baseline-silver-rYYMMDDhhmmss` |
| `gold_run_id` | `<benchmark_id>-baseline-gold-rYYMMDDhhmmss` |
| `feature_run_id` | `<benchmark_id>-baseline-feat-rYYMMDDhhmmss` |
| `project_id`, `region` | the project and `asia-southeast1` |
| `data_bucket` | `ewallet-data-ai-platform-hg26-rehearsal-5gb-data` (the default; any other value is refused) |
| `dataproc_ttl` | `8h` |
| `terraform_output_json` | empty to use `terraform output`, or an absolute path to a saved copy |
| `execution_approval` | `EXECUTE-PAID-DATAPROC:<benchmark_id>:baseline:rehearsal-5gb-v1:<first 12 of git_sha>` |

Any other approval value makes every task refuse to run. Tasks run one at a time.

## Part 4 — Optimized

After reviewing the Baseline outputs and evidence, trigger a second DagRun with
`spark_profile=optimized`, the same `benchmark_id`, the same `source_manifest_sha256`,
new `-optimized-` run IDs and its own approval token. It reads only its own upstream
outputs.

With `B=ewallet-data-ai-platform-hg26-rehearsal-5gb-data`:

**Comparison afterwards** (read-only; run with spark-submit or as a Dataproc batch, after
approval). It takes the two finished campaign manifests:
```bash
python -m ops.gcp.compare_campaign_outputs \
  --baseline-manifest-uri gs://$B/lakehouse/campaigns/<benchmark_id>/baseline/campaign_manifest.json \
  --optimized-manifest-uri gs://$B/lakehouse/campaigns/<benchmark_id>/optimized/campaign_manifest.json \
  --report-uri gs://$B/lakehouse/campaigns/<benchmark_id>/comparison.json
```
The report's verdict is `SEMANTIC_EQUIVALENT`, `SEMANTIC_MISMATCH` or `COMPARISON_INCOMPLETE`.
Only `created` (five feature tables) and `created_timestamp` (`feat_user_90d`) are excluded;
each run's validators check those against its own recorded clock. Performance comes from
`python -m ops.gcp.spark_stage_report` on the event logs.

## Part 5 — Evidence checkpoints

| Where | What |
|---|---|
| Airflow | DagRun graph; each group's task logs; `preflight` output |
| Dataproc | `gcloud dataproc batches describe <id>`: state, runtime, times. Transformation batch = the run ID; validator batch = `<run>-v<attempt>` |
| Spark UI | Link from the batch; event logs in `gs://<bucket>/spark-events/` |
| Validation | `gs://<bucket>/lakehouse/runs/<run>/evidence/validation-<scope>-a<attempt>.json` |
| Bronze | `gs://<bucket>/lakehouse/runs/<bronze run>/evidence/source_to_bronze.json` (has the Spark application ID) |
| Campaign | `gs://<bucket>/lakehouse/campaigns/<benchmark_id>/<profile>/campaign_manifest.json` |
| GCS | `runs/<run>/{bronze,silver,gold}/`, `_delta_log` |

**Finding each Spark application:** every transformation and validator writes
`runs/<run>/evidence/spark_execution-<batch>.json` at startup (real application ID and event-log
directory), and the campaign keeps `execution_provenance-<batch>.json` beside it, also for failed
batches. The manifest repeats this per stage. To see all stages at any time, read-only:
```bash
python -m ops.gcp.spark_provenance --dataset-version rehearsal-5gb-v1 --benchmark-id <id> \
  --spark-profile baseline --git-sha <sha> --source-manifest-sha256 <sha256> \
  --bronze-run-id <id> --silver-run-id <id> --gold-run-id <id> --features-run-id <id> \
  --project-id ewallet-data-ai-platform-hg26 --region asia-southeast1 --data-bucket "$B"
```
A field is null when Dataproc or the job did not provide it. The event log is
`gs://$B/spark-events/<application id>` (listed only when it exists); `history_server_uri` comes
from the batch's own endpoints and is null if Dataproc gives none.

The Serverless Spark UI may not stay available after a batch ends, so capture it
during or right after the run, or from the event logs.

## Part 6 — Recovery

The output prefixes are immutable, so a stage is never re-run over its own output.

- **Failed before any output** (quota, preflight, source gate): fix the cause and clear
  the failed task.
- **Failed validator:** read its report. If the data is at fault, the campaign stops
  there. If the batch failed for an infrastructure reason, clear the task: the Airflow
  try number gives a new batch ID and report (`-v2`, `-a2`, up to 9 attempts).
- **Failed or partial transformation:** clearing the task is refused, because the
  output prefix exists. Trigger a new DagRun with new run IDs and a new approval token.
  Old outputs stay in place; do not delete them as routine recovery.
- **Mismatch at the source gate:** stop and investigate. Do not publish over the dataset.
