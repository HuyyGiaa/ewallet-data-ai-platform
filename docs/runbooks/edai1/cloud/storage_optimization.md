# Cloud Delta storage optimization runbook

Procedure for the isolated Delta layout benchmark (compaction, Z-order and partition pruning) on Managed Service for Apache Spark. The design and the official 5GB results are in [STORAGE_OPTIMIZATION.md](../../../edai1/storage/STORAGE_OPTIMIZATION.md). The screenshot capture plan for the 5GB evidence is in [DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md](../DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md), Stages 3–4. This page holds the commands that apply to any dataset size.

## Prerequisites

| Item | Requirement |
|---|---|
| Source campaign | A completed Spark A/B campaign whose committed evidence passes `python3 -m ops.edai1_verifier --spec <workload spec> --evidence-dir <campaign evidence dir>`. The plan takes table URIs from that campaign's `campaign_manifest.json`, never from the command line. |
| Workload spec | A `workload-*.json` naming the campaign, dataset version and source Spark commit (5GB: `images/edai1-campaign-verifier/workload-5gb.json`). |
| Tool commit | The commit that runs the benchmark, checked out with a clean tracked tree. The submitter refuses anything else. |
| Runtime | Dataproc Serverless 2.2 (Spark 3.5.3) with the pinned `delta-spark_2.13:3.2.0`, `delta-storage:3.2.0` and `antlr4-runtime:4.9.3` jars under `tmp/dataproc-delta-dependencies/`. |
| Credentials | `gcloud` ADC for the project. Terraform output (read-only) for the service account, subnet and buckets. |
| Python | `ai_platform/features/feast` uv environment (`deltalake`, `pyarrow`), kept outside the checkout. |

The current campaign is the 5GB rehearsal below. The tool has no built-in dataset: a larger campaign uses the same commands with its own verified evidence folder, workload spec and bucket. The Z-order arm rewrites each selected table in full, so cost grows with the table size. Check the Dataproc DCU quota and sleep the GKE lab before submitting.

## 1. Environment

```bash
cd "$REPO"                                   # checkout at the tool commit
git status --short                           # must print nothing for tracked files
export TOOL_GIT_SHA="$(git rev-parse HEAD)"
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/ewallet-venvs/feast"
export PYTHONPATH="$REPO"
export RUN_TS="$(date -u +%Y%m%dt%H%M%S | tr 'T' 't')"
export OUT="$HOME/ewallet-evidence/storage-$RUN_TS"; mkdir -p "$OUT"

# Dataset-specific values (5GB shown).
export E=docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456
export SPEC=images/edai1-campaign-verifier/workload-5gb.json
export DATASET_VERSION=rehearsal-5gb-v1
export SOURCE_SPARK_GIT_SHA=aae22b08eee20fa055c4aefc8d23c14cc3ae5f8c
export DATA_BUCKET=ewallet-data-ai-platform-hg26-rehearsal-5gb-data
export STORAGE_BENCHMARK_ID="storage-5gb-$RUN_TS"

: "${E:?}" "${SPEC:?}" "${DATASET_VERSION:?}" "${SOURCE_SPARK_GIT_SHA:?}" "${DATA_BUCKET:?}" "${STORAGE_BENCHMARK_ID:?}"
python3 -m ops.edai1_verifier --spec "$SPEC" --evidence-dir "$E"     # verdict PASS
```

## 2. Plan (read-only)

```bash
./scripts/inspect_storage_layout.sh \
  --benchmark-id "$STORAGE_BENCHMARK_ID" \
  --dataset-version "$DATASET_VERSION" \
  --source-spark-git-sha "$SOURCE_SPARK_GIT_SHA" \
  --tool-git-sha "$TOOL_GIT_SHA" \
  --data-bucket "$DATA_BUCKET" \
  --campaign-evidence-dir "$E" --workload-spec "$SPEC" \
  --table dp2-gold.fact_transactions --table dp2-gold.obt_transaction_enriched \
  --output-dir "$OUT/storage-plan"
```

The command prints one JSON line per table: Delta version, files, bytes and the data-derived workload parameters. It writes `plan.json` and `layout_before.json`. It reads Delta metadata and two columns of the window partitions, and writes nothing to GCS.

**Decision rules recorded in the plan** (from `data_platform/storage/cloud_optimization.py`):
- **Compaction candidate:** at least 256 MiB, at least 32 files, and either half of the files below 16 MiB or a median below 32 MiB.
- **Daily partition kept as healthy:** median partition at least 64 MiB, with low skew.
- **Z-order predicted useful:** at least 90 % min/max coverage and at least 8 distinct file-level bounds.

The job executes every arm regardless of these predictions, so they are recorded, not enforced.

## 3. Dry run

```bash
terraform -chdir=infra/terraform/gcp/environments/dev output -json > "$OUT/terraform-output.json"
./scripts/run_storage_benchmark.sh --plan "$OUT/storage-plan/plan.json" \
  --terraform-output-json "$OUT/terraform-output.json" | tee "$OUT/storage-dry-run.txt"
```

Review before approving:
- `EXECUTION: DRY_RUN`;
- `INPUT` paths name the campaign's optimized run at the planned Delta version;
- every output is below `gs://$DATA_BUCKET/lakehouse/benchmarks/storage/$STORAGE_BENCHMARK_ID/`;
- `TOOL_GIT_SHA` equals `git rev-parse HEAD` and differs from `SOURCE_SPARK_GIT_SHA`.

## 4. Execute (paid; owner approval)

```bash
./scripts/run_storage_benchmark.sh --plan "$OUT/storage-plan/plan.json" \
  --terraform-output-json "$OUT/terraform-output.json" --execute | tee "$OUT/storage-execute.txt"
gcloud dataproc batches describe "$STORAGE_BENCHMARK_ID-storage" --region=asia-southeast1 \
  --format='value(state,stateTime)'                                   # SUCCEEDED
gcloud storage cp -r "gs://$DATA_BUCKET/lakehouse/benchmarks/storage/$STORAGE_BENCHMARK_ID/evidence" \
  "$OUT/storage-evidence"
python3 -m ops.gcp.storage_benchmark_report summary --plan "$OUT/storage-plan/plan.json" \
  --evidence-dir "$OUT/storage-evidence/evidence" | tee "$OUT/storage-summary.txt"
```

The job creates three shallow clones per table (`baseline`, `compacted`, `optimized`) pinned to the planned version. It never writes the source tables, never runs `VACUUM`, and uploads evidence create-only.

**Failure.** Keep everything and retry with a new `STORAGE_BENCHMARK_ID`. Never reuse, overwrite or delete a partial benchmark prefix. Whatever the verdicts, they are the result: do not rerun to change them.

## 5. Trino verification (read-only)

```bash
python3 -m ops.gcp.storage_benchmark_report trino-sql --plan "$OUT/storage-plan/plan.json" \
  --output-dir "$OUT/storage-trino"
export EDAI1_GCP_ADC_FILE="$HOME/.config/gcloud/application_default_credentials.json"
docker compose -f infra/docker/schema-viz/compose.yml up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' edai1-schema-viz-trino)" = healthy ]; do sleep 5; done
infra/docker/schema-viz/run_sql.sh registrar "$OUT/storage-trino/storage_register.sql" | tee "$OUT/storage-register.txt"
# DBeaver: Trino, 127.0.0.1:8091, user viewer, no password; run storage_queries.sql block by block.
docker compose -f infra/docker/schema-viz/compose.yml stop
```

`storage_queries.sql` is the one query script per benchmark. It is generated, not edited by hand: regenerate it with the command above instead of saving changes from DBeaver. Its blocks are:
- ST01 `$history` of the three arms;
- ST02/ST04 file layout per arm, plus the ST04 file locations of the baseline and Z-order arms;
- ST05/ST07 `EXPLAIN ANALYZE` of the merchant lookup and the date range on each arm;
- ST09, per table: for each of `pruned`, `stats_only` and `full_scan`, the plain aggregate (`-- ST09 result:`), then its `EXPLAIN ANALYZE` (`-- ST09 partition pruning:`).

**Time zone.** `event_date` is the UTC day of `timestamp` (verified on all 18,600,000 rows of both Gold tables), and the Spark job refuses a non-UTC session. Trino reads `timestamp` as `timestamp(3) with time zone` and interprets a zone-less literal in the client session zone, which DBeaver sets to the desktop zone. The generated SQL therefore writes `TIMESTAMP '… UTC'` and `format_datetime("timestamp" AT TIME ZONE 'UTC', …)`, so every ST09 side returns the same rows in any session zone. `tests/integration/test_storage_trino_timezone.py` checks this in Trino under UTC and UTC+7. For ad-hoc queries, write zoned literals or run `SET TIME ZONE 'UTC';` first.

## 6. Evidence retention

| Keep in Git (under `docs/evidence/edai1/storage/<scope>/`) | Keep outside Git |
|---|---|
| `plan.json`, `layout_before.json`, `evidence/*.json`, `comparison.csv`, `storage-summary.txt`, the dry-run, execute and register logs, the Trino SQL, screenshots with `SHA256SUMS` | Delta clones (benchmark prefix), Spark event logs (`gs://$DATA_BUCKET/spark-events`), any Parquet |
