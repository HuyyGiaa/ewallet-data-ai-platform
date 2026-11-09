# EDAI-1 DP1–DP3 campaign runbook

Audience: the project owner, operating the 5GB rehearsal.

The workflow is the DAG `edai1_data_pipeline_campaign`. One DagRun carries one Spark
profile from the immutable Source dataset through Bronze, Silver, Core Gold and DP3
features. Every stage is followed by a physical Delta check and a semantic validator.
There is no winner selection and no promotion. Baseline and Optimized are two
separate DagRuns that read the same Source.

Nothing in this runbook has been run on Dataproc yet.

## Part 0 — Unblock Source publication (owner action)

`rehearsal-5gb-v1` must live in its own bucket: the registry refuses it in
`ewallet-data-ai-platform-hg26-data`, and no rehearsal bucket exists. Creating one
is an owner action (Terraform and IAM).

1. Create the bucket in `asia-southeast1` through Terraform and grant the Dataproc
   Spark service account object access to it. Name it, for example,
   `ewallet-data-ai-platform-hg26-rehearsal-5gb` and set `REHEARSAL_BUCKET` to it.
2. Write a Terraform outputs file that names the rehearsal bucket:
   ```bash
   terraform -chdir=infra/terraform/gcp/environments/dev output -json \
     | jq --arg b "$REHEARSAL_BUCKET" '.data_bucket_name.value = $b' > /absolute/path/rehearsal-outputs.json
   ```
3. Publish (one shard at a time per worker, generated, uploaded, verified, then its
   local staging is removed):
   ```bash
   EWALLET_DATA_BUCKET=$REHEARSAL_BUCKET GCP_PROJECT_ID=ewallet-data-ai-platform-hg26 \
   DATAHUB_GMS_URL=http://datahub.invalid \
   uv run --locked --project data_platform/generation python -m ops.gcp.generate_source_dataset \
     --config data_platform/generation/config/settings_merchant_history.yaml \
     --environment gcp --dataset-version rehearsal-5gb-v1 --global-seed 7001 --shard-count 186 \
     --users-per-shard 12500 --merchants-per-shard 300 --transactions-per-shard 100000 \
     --workers 2 --staging-dir /path/with/free/space --execute
   ```
   Without `--execute` the same command is a dry run. Expected plan digest:
   `422c15a169165158fcd7d432a7943d50812b5f3e55e7d0c6aec7c457371c6c83`. Re-running after
   an interruption resumes: verified shards are skipped and conflicting objects stop
   the publication. Allow about 1.5 hours with 2 workers.

## Part 1 — Verify the Source dataset

```bash
python -m ops.gcp.verify_source_dataset --bucket "$REHEARSAL_BUCKET" \
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
| `data_bucket` | the rehearsal bucket |
| `dataproc_ttl` | `8h` |
| `terraform_output_json` | absolute path of `rehearsal-outputs.json` |
| `execution_approval` | `EXECUTE-PAID-DATAPROC:<benchmark_id>:baseline:rehearsal-5gb-v1:<first 12 of git_sha>` |

Any other approval value makes every task refuse to run. Tasks run one at a time.

## Part 4 — Optimized

After reviewing the Baseline outputs and evidence, trigger a second DagRun with
`spark_profile=optimized`, the same `benchmark_id`, the same `source_manifest_sha256`,
new `-optimized-` run IDs and its own approval token. It reads only its own upstream
outputs.

**Comparison afterwards** (read-only; run with spark-submit or as a Dataproc batch,
one per stage, after approval):
```bash
python -m ops.gcp.compare_campaign_outputs --stage dp2-gold --dataset-version rehearsal-5gb-v1 \
  --baseline-root gs://$REHEARSAL_BUCKET/lakehouse/runs/<baseline gold run>/gold \
  --optimized-root gs://$REHEARSAL_BUCKET/lakehouse/runs/<optimized gold run>/gold \
  --report-uri gs://$REHEARSAL_BUCKET/lakehouse/campaigns/<benchmark_id>/comparison-dp2-gold.json
```
It compares rows by logical key, ignores the run-specific `created`, and writes
PASS or DIFFERENT. Performance comes from `python -m ops.gcp.spark_stage_report` on the
event logs.

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
