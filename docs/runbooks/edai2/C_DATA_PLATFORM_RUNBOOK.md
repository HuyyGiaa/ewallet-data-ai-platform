# Data Platform Runbook (C1, C2, C3)

This runbook takes the owner from a reviewed branch to cloud evidence for EDAI-2 rows 11 and 12
and the EDAI-1 DP2 Gold, schema and Spark UI gaps. Ownership rules and findings are in the
[integration contract](../../edai2/platform/C_DATA_PLATFORM_INTEGRATION.md).

Every section marks commands **READ-ONLY** or **MUTATING**. Run nothing marked MUTATING until the
matching experiment in [section 7](#7-cloud-approval-package) is approved. A step's result counts
as evidence only when it ran against the real component named in that step.

## 1. Shared setup

**READ-ONLY.** Run from the repository root on `feature/c-data-platform-completion`.

```bash
export PATH="$HOME/miniconda3/envs/fintech/bin:$PATH"
export PROJECT=ewallet-data-ai-platform-hg26
export BUCKET=${PROJECT}-data
export KUBE_CONTEXT=gke_${PROJECT}_asia-southeast1-b_ewallet-dev
export SOURCE_SHA=$(git rev-parse HEAD)
gcloud config get-value project          # expect ewallet-data-ai-platform-hg26
curl -s https://api.ipify.org; echo      # this machine's address
gcloud container clusters describe ewallet-dev --zone asia-southeast1-b --project "$PROJECT" \
  --format='value(masterAuthorizedNetworksConfig.cidrBlocks[].cidrBlock)'
```

If your address is not listed, `kubectl` times out. Adding it is experiment E0.

Once the cluster is reachable, confirm its state. **READ-ONLY.**

```bash
kubectl --context "$KUBE_CONTEXT" get nodes
kubectl --context "$KUBE_CONTEXT" -n feature-store get statefulset,svc
kubectl --context "$KUBE_CONTEXT" -n ml-platform get serviceaccount training
kubectl --context "$KUBE_CONTEXT" -n ml-platform get secret feast-online-redis -o name
kubectl --context "$KUBE_CONTEXT" -n ml-platform get resourcequota,limitrange,networkpolicy
scripts/mlops/cluster_capacity.sh
```

**Stop if** the `feast-online-redis` Secret is missing, a quota leaves less than 2 CPU and 8 GiB, or
a NetworkPolicy would block traffic inside `ml-platform`.

Prepare the local environments. **Local only.**

```bash
uv sync --locked --project ai_platform/features/feast
uv sync --locked --project infra/docker/streaming-sinks
```

## 2. C1: Airflow incremental materialization

### 2.1 Why this run uses a separate registry

The live registry already reaches `2026-06-01T00:00:00Z`, past the snapshot's last event at
`2026-05-31T23:59:58.617Z`, so it has no new window. A new registry on a separate Redis database
gives a real incremental window. Live Redis keys and the live registry are not touched.

| Variable | Value |
|---|---|
| Snapshot | `canonical-training-v1-20261028-101106-recovered` |
| Feature run | `ab-20261025134342-optimized-feat-r261026025955` |
| New registry id | `online-c1-20261107` (choose a new id for each attempt) |
| Redis Secret | `feast-online-redis-verify` (selects `db=1`) |
| Base window | `2026-05-31T21:00:00Z` to `22:00:00Z` |
| Airflow target | `2026-05-31T23:00:00Z`, loading `22:00` to `23:00` |

```bash
export DATASET=canonical-100gb-v1
export FEATURE_RUN=ab-20261025134342-optimized-feat-r261026025955
export SNAPSHOT=canonical-training-v1-20261028-101106-recovered
export REGISTRY_ID=online-c1-20261107
export FEAST_OFFLINE_DATA_ROOT=gs://$BUCKET/features/offline/snapshots/$DATASET/$FEATURE_RUN/$SNAPSHOT
export ONLINE_REGISTRY_URI=gs://$BUCKET/features/registries/$DATASET/online/$REGISTRY_ID/registry.db
export FEAST_REDIS_SECRET=feast-online-redis-verify
export FEAST_ONLINE_IMAGE=<digest from E1>
```

### 2.2 Prepare

**MUTATING (E1).** Build the operator image, create the Secret without printing it, and load the
base window.

```bash
docker build -f ai_platform/inference/Dockerfile \
  -t asia-southeast1-docker.pkg.dev/$PROJECT/ewallet-platform/fraud-api:c1-$SOURCE_SHA .
docker push asia-southeast1-docker.pkg.dev/$PROJECT/ewallet-platform/fraud-api:c1-$SOURCE_SHA

kubectl --context "$KUBE_CONTEXT" -n ml-platform get secret feast-online-redis \
    -o jsonpath='{.data.connection_string}' | base64 -d | sed 's/$/,db=1/' \
  | kubectl --context "$KUBE_CONTEXT" -n ml-platform create secret generic feast-online-redis-verify \
    --from-file=connection_string=/dev/stdin

scripts/mlops/feast_online_job.sh c1-apply-20261107 --execute -- apply
scripts/mlops/feast_online_job.sh c1-base-20261107 --execute -- \
  materialize --start 2026-05-31T21:00:00+00:00 --end 2026-05-31T22:00:00+00:00
```

**Expected:** the last line of each command is JSON, and the second reports four views.

### 2.3 Run through Airflow

**Local control plane.** Start Airflow and confirm the DAG. **READ-ONLY.**

```bash
scripts/start_airflow_local.sh
~/miniconda3/envs/airflow/bin/airflow dags list | grep feast_incremental_materialization
~/miniconda3/envs/airflow/bin/airflow dags list-import-errors
```

Write the shared run configuration, then a dry run. It renders Job manifests and prints the
approval token for this window. It starts no Job. **READ-ONLY.**

```bash
cat >/tmp/c1.json <<EOF
{"feature_run_id":"$FEATURE_RUN","feast_snapshot_id":"$SNAPSHOT","online_registry_id":"$REGISTRY_ID",
 "target_end":"2026-05-31T23:00:00Z","feast_online_image":"$FEAST_ONLINE_IMAGE",
 "feast_redis_secret":"feast-online-redis-verify","git_sha":"$SOURCE_SHA"}
EOF
~/miniconda3/envs/airflow/bin/airflow dags unpause feast_incremental_materialization
~/miniconda3/envs/airflow/bin/airflow dags trigger feast_incremental_materialization \
  --conf "$(cat /tmp/c1.json)"
```

Read `approval_token_for_this_window` from
`~/ewallet-evidence/c1-feast-incremental/<run id>/01-config.json`.

**MUTATING (E1).** Execute. The token works only for this exact window.

```bash
python3 - <<'PY' >/tmp/c1-exec.json
import json
value = json.load(open("/tmp/c1.json"))
value.update(mode="execute", approval="approve-<window id from the dry run>")
print(json.dumps(value))
PY
~/miniconda3/envs/airflow/bin/airflow dags trigger feast_incremental_materialization \
  --conf "$(cat /tmp/c1-exec.json)"
```

Trigger the same configuration again to prove idempotence. **Expected:** `outcome` is `COMPLETED`
and no `feast-inc-*` Job is created.

### 2.4 PASS and FAIL

| Check | PASS |
|---|---|
| `07-provenance.json` | `outcome` is `NEW_WINDOW` on the first run, `COMPLETED` on the repeat |
| `04-materialize.json` | `status` is `PASS` and four views ran from `22:00` to `23:00` |
| `05-completion.json` | every `materialized_end` is at least `23:00` |
| `06-readback.json` | `summary.comparisons` above 0 and `mismatch_count` 0 |
| Airflow | all seven tasks green in Grid and Graph |

### 2.5 Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `was never materialized` | No base window | Run the base window in 2.2 |
| `after the snapshot's last event` | Target too late | Choose a target inside the snapshot |
| `exceeds max_window` | Window above the limit | Use a smaller window or raise `max_window_hours` |
| `C1_APPROVAL` error | Token from another window | Repeat the dry run |
| `belongs to a different request` | Run id reused with new values | Trigger a new run |
| `Python module is not in the Airflow allowlist` | Old checkout in Airflow | Restart Airflow on this branch |
| Job `Failed` | Operator error | `kubectl -n ml-platform logs job/<name>`, then retry; the next attempt is created |
| `readback` fails on timestamps | Rows outside the window | Keep the artifact and do not rerun; it is a real finding |

### 2.6 Cleanup and rollback

Materialization writes only to `db=1` and the new registry. **MUTATING.**

```bash
kubectl --context "$KUBE_CONTEXT" -n feature-store exec statefulset/redis -- \
  sh -c 'redis-cli -a "$REDIS_PASSWORD" -n 1 --no-auth-warning DBSIZE'
# Confirm that the number above is the verification database, then:
kubectl --context "$KUBE_CONTEXT" -n feature-store exec statefulset/redis -- \
  sh -c 'redis-cli -a "$REDIS_PASSWORD" -n 1 --no-auth-warning FLUSHDB'
kubectl --context "$KUBE_CONTEXT" -n ml-platform delete secret feast-online-redis-verify
```

Never run `FLUSHDB` without `-n 1`. Database 0 serves the Fraud API. Jobs expire after 24 hours.
Keep the registry object as evidence.

### 2.7 Evidence

Copy `~/ewallet-evidence/c1-feast-incremental/<run id>/` to
`docs/evidence/edai2/mlops/c1_feast_incremental/`.

| Screenshot | Shows |
|---|---|
| `c1_01_dag_enabled.png` | DAG listed and unpaused |
| `c1_02_grid_success.png` | Grid with seven green tasks |
| `c1_03_graph.png` | Graph with the task order |
| `c1_04_gke_job.png` | `feast-inc-*` Job succeeded in the GKE console |
| `c1_05_readback.png` | `06-readback.json` with comparisons and zero mismatches |
| `c1_06_repeat_completed.png` | Second run reporting `COMPLETED` |

## 3. C2: Flink offline and online stores

### 3.1 Local rehearsal

**Local only.** This uses real Redpanda, Flink, Feast and a loopback Redis. It proves readiness, not
a cloud result. Keep at least 8 GiB free, and stop DP2 or vLLM work first.

```bash
docker run -d --name c-redis-test -p 127.0.0.1:16399:6379 \
  redis@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952
docker compose -f infra/docker/flink-benchmark/compose.yml build jobmanager
scripts/streaming/block_a_verify.sh up
scripts/streaming/block_a_verify.sh run verify-owner-1
scripts/streaming/block_a_verify.sh replay verify-owner-1
BLOCK_A_PHASE_DELAY=45 BLOCK_A_RESTART_AFTER_SECONDS=150 \
  scripts/streaming/block_a_verify.sh run verify-owner-2
scripts/streaming/block_a_verify.sh down
docker rm -f c-redis-test
```

**Expected:** each run ends with `"status": "PASS"`. A namespace can be used once. The second run
restarts after the late phases, because watermarks do not survive a restore, as the
[contract](../../edai2/platform/C_DATA_PLATFORM_INTEGRATION.md#5-c2-flink-dual-store-path) explains.

### 3.2 Cloud run

**MUTATING (E2).** Hosting is a decision for the owner, because Redpanda and Flink have never run in
the cloud. These manifests use a bounded stack in `ml-platform`.

```bash
export FLINK_IMAGE=<digest from E2>
export SINKS_IMAGE=<digest from E2>
export REGISTRY_ID=online-c2-20261107
export ONLINE_REGISTRY_URI=gs://$BUCKET/features/registries/$DATASET/online/$REGISTRY_ID/registry.db
export HISTORY_ROOT=gs://$BUCKET/streaming-history/c2-20261107
export FEAST_REDIS_SECRET=feast-online-redis-verify

scripts/streaming/block_a_cloud.sh render > /tmp/block-a.yaml        # READ-ONLY
kubectl --context "$KUBE_CONTEXT" apply --dry-run=server -f /tmp/block-a.yaml  # READ-ONLY

scripts/mlops/feast_online_job.sh c2-apply-20261107 --execute -- apply
scripts/streaming/block_a_cloud.sh deploy --execute
scripts/streaming/block_a_cloud.sh run verify-gke-1 --execute
scripts/streaming/block_a_cloud.sh collect verify-gke-1 --execute
```

Open the Flink UI, then read its metrics. **READ-ONLY.**

```bash
kubectl --context "$KUBE_CONTEXT" -n ml-platform port-forward svc/block-a-flink-jobmanager 18081:8081
curl -s localhost:18081/jobs/overview
```

### 3.3 PASS and FAIL

| Check | PASS |
|---|---|
| `consistency-report.log` | `status` is `PASS`, 189 features compared, `timestamp_mismatches` 0 |
| Dispositions | `disposition_problems` is empty |
| Flink metrics | `late_accepted_total` 1, `too_late_total` 1, `duplicates_detected_total` 2, `dlq_total` 1 |
| Watermark | `currentOutputWatermark` is in June 2026, not the wall clock |
| GCS | `gsutil ls "$HISTORY_ROOT/runs/verify-gke-1/**"` lists objects |

The feature count holds for this scenario's seed family; a different namespace can change it
slightly. A `PASS` with fewer than 100 features should be investigated.

### 3.4 Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `expected LATE_ACCEPTED, saw ON_TIME` | Watermark had not moved between phases | Raise `--phase-delay-seconds` |
| Watermark near today's date | Old image without the event-time fix | Rebuild the Flink image |
| `namespace already used` | Topics exist | Use a new namespace |
| Zero Feast writes | Sink stopped before timers fired | Check `--max-idle-seconds` against the publish time |
| Sink cannot reach Redis | Pod label or NetworkPolicy | The pod must keep `app.kubernetes.io/name: feast-online-operator` |
| `MISSING` features | Feast writer failed | Read the `block-a-feast` log |
| `TIMESTAMP_MISMATCH` | A real consistency failure | Keep the report as a finding |

### 3.5 Cleanup and rollback

**MUTATING.** The stack is labelled, and its storage disappears with it.

```bash
scripts/streaming/block_a_cloud.sh cleanup --execute
```

Clear `db=1` as in 2.6. Keep `$HISTORY_ROOT`, which is immutable evidence.

### 3.6 Evidence

Copy `~/ewallet-evidence/c2-block-a/<namespace>/` to
`docs/evidence/edai2/mlops/c2_flink_dual_store/`.

| Screenshot | Shows |
|---|---|
| `c2_01_flink_job_running.png` | Job graph, `RUNNING` |
| `c2_02_watermarks.png` | Watermark panel with an event-time value |
| `c2_03_late_metrics.png` | `late_accepted_total` and `too_late_total` |
| `c2_04_redpanda_topics.png` | Namespaced topics and offsets |
| `c2_05_gcs_history.png` | History objects by run and partition |
| `c2_06_consistency_pass.png` | Report with `PASS` and 189 features |

## 4. C3: Gold equivalence

**READ-ONLY.** This reads the two Gold outputs and creates no Dataproc batch. It downloads about
4.2 GB and needs about 12 GB of free memory.

```bash
export BASE=gs://$BUCKET/lakehouse/runs/ab-20261031035745-baseline-gold-r261031035746/gold
export OPT=gs://$BUCKET/lakehouse/runs/ab-20261031035745-optimized-gold-r261031035746/gold
PYTHONPATH=. uv run --locked --project ai_platform/features/feast python \
  -m ops.gcp.validate_gold_row_equivalence \
  --baseline-root "$BASE" --optimized-root "$OPT" \
  --table feat_user_90d --table opt_merchant_performance \
  --exact-equivalence docs/evidence/dp2_method2_ab-20261031035745/raw/dp2-gold-equivalence.json \
  --output-dir ~/ewallet-evidence/c3-gold-equivalence/$(date -u +%Y%m%dT%H%M%SZ)
```

| Result | Meaning |
|---|---|
| `TOLERANCE_EQUIVALENT` | Only allowlisted float columns differ, within tolerance. The exact gate stays failed and visible |
| `NOT_EQUIVALENT` | A real failure. Report it, and do not change the policy to pass |
| `EXACT_EQUIVALENT` | Only possible with a new exact gate artifact |

The artifacts are `dp2-gold-row-equivalence.json` and `dp2-gold-final-equivalence.json`. Adopting
the optimized output is a separate owner decision. This tool does not select a winner.

**Screenshot:** `c3_01_gold_verdict.png`, the final JSON with the status and digests.

## 5. C3: schema diagrams

**READ-ONLY.** Reads Delta logs only.

```bash
python -m ops.schema.export_observed_schemas --bucket "$BUCKET" \
  --bronze-run-id ab-20261029174838-baseline-bronze-r261029174838 \
  --silver-run-id ab-20261031035745-baseline-silver-r261031035745 \
  --gold-run-id ab-20261031035745-baseline-gold-r261031035746 \
  --feature-run-id ab-20261025134342-optimized-feat-r261026025955 \
  --output docs/edai1/schema/generated/observed_schemas.json
python -m ops.schema.render_schema_diagrams
```

The files are listed in [SCHEMA_DIAGRAMS.md](../../edai1/schema/generated/SCHEMA_DIAGRAMS.md).

To get a DBeaver ER diagram, load the import file into a scratch database. **Local only.**

```bash
docker run -d --name erd-scratch -e POSTGRES_PASSWORD=scratch -p 127.0.0.1:15432:5432 postgres:16
docker exec -i erd-scratch psql -U postgres < docs/edai1/schema/generated/gold_erd_import.sql
# In DBeaver: connect to localhost:15432, open schema gold, then the ER Diagram tab.
docker rm -f erd-scratch
```

`gold.dbml` can also be pasted into dbdiagram.io. Label any export as logical: the keys exist only
in that scratch database, not in the Delta tables.

**Screenshots:** `c3_02_erd_gold.png` and `c3_03_erd_features.png`.

## 6. C3: Spark UI evidence

Dataproc Serverless keeps event logs in `gs://$BUCKET/spark-events/`, and no persistent History
Server exists. Both methods below are **READ-ONLY** on the cloud.

| Run | Application id |
|---|---|
| DP2 Silver baseline | `app-20261031035947993-0068` |
| DP2 Silver optimized | `app-20261031044729565-0300` |
| DP2 Gold baseline | `app-20261031061021397-0934` |
| DP2 Gold optimized | `app-20261031082220536-0729` |
| DP3 canonical | `app-20261026030136645-0407` |

The four DP2 entries were confirmed: each report's `runtime_seconds` matches the published
application runtime (2,656.6, 3,496.5, 7,810 and 6,005 seconds). The DP3 entry comes from its
application name and start time, so confirm it before quoting it.

### 6.1 Metrics report

```bash
mkdir -p ~/ewallet-evidence/c3-spark && cd ~/ewallet-evidence/c3-spark
gsutil cp gs://$BUCKET/spark-events/app-20261031061021397-0934.zstd \
          gs://$BUCKET/spark-events/app-20261031082220536-0729.zstd .
cd - >/dev/null
OUT=~/ewallet-evidence/c3-spark
python -m ops.gcp.spark_stage_report report --event-log $OUT/app-20261031061021397-0934.zstd --output $OUT/gold-baseline.json
python -m ops.gcp.spark_stage_report report --event-log $OUT/app-20261031082220536-0729.zstd --output $OUT/gold-optimized.json
python -m ops.gcp.spark_stage_report compare --baseline $OUT/gold-baseline.json --optimized $OUT/gold-optimized.json \
  --input-identity ab-20261031035745-baseline-silver-r261031035745 --output $OUT/gold-comparison.json
```

Each report gives runtime, stage durations, shuffle read and write, spill, a skew flag, executor
utilization and SQL plan fingerprints. `--input-identity` must name the input both runs read. The
pair manifest holds it, in `upstream_run_ids`. Comparing runs over different inputs does not prove
an improvement, and a slower optimized run is a valid result to report.

### 6.2 Spark UI screenshots

Run a local History Server over the downloaded logs.

```bash
export SPARK_HOME=$(python -c "import pyspark, os; print(os.path.dirname(pyspark.__file__))")
export SPARK_HISTORY_OPTS="-Dspark.history.fs.logDirectory=file://$HOME/ewallet-evidence/c3-spark -Dspark.history.ui.port=18080"
"$SPARK_HOME/sbin/start-history-server.sh"
# Open http://localhost:18080, then stop it:
"$SPARK_HOME/sbin/stop-history-server.sh"
```

| Screenshot | Page |
|---|---|
| `c3_04_spark_jobs_<run>.png` | Jobs, with the application id visible |
| `c3_05_spark_stages_<run>.png` | Stages sorted by duration, with shuffle columns |
| `c3_06_spark_skew_<run>.png` | Task summary of the most skewed stage |
| `c3_07_spark_executors_<run>.png` | Executors |
| `c3_08_spark_sql_<run>.png` | SQL plan of the main query |

## 7. Cloud approval package

Common target: project `ewallet-data-ai-platform-hg26`, cluster `ewallet-dev`
(`asia-southeast1-b`), namespace `ml-platform`, bucket `ewallet-data-ai-platform-hg26-data`.
Source: the HEAD of `feature/c-data-platform-completion`.

Costs are estimates from prior runs. No billing data was read.

### E0: control-plane access

| Item | Detail |
|---|---|
| Mutation | Add this machine's `/32` to `masterAuthorizedNetworks` |
| Blast radius | Control-plane network allowlist |
| Cost | None |
| Dry run | Section 1 shows current ranges |
| Rollback | Restore the previous ranges |

Keep the two existing ranges, because `--master-authorized-networks` replaces the whole list:

```bash
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --project "$PROJECT" \
  --enable-master-authorized-networks \
  --master-authorized-networks 113.188.119.38/32,113.188.185.247/32,<this machine>/32
```

### E1: C1 materialization

| Item | Detail |
|---|---|
| Mutations | One image in Artifact Registry; Secret `feast-online-redis-verify`; new registry object; Jobs in `ml-platform`; keys in Redis `db=1` |
| Not touched | Redis `db=0`, the live registry, the Fraud API, KServe |
| Blast radius | Redis memory is shared and capped at 768 MB with `noeviction`, so a full instance rejects writes, including to `db=0` |
| Cost and duration | Existing nodes; about 25 minutes of Jobs, based on 11 minutes per one-hour window on 31 October |
| Dry run | DAG default mode; `feast_online_job.sh` without `--execute` |
| Verify | Section 2.4 |
| Rollback | Section 2.6 |

**Check first:** `INFO memory` on Redis. Two one-hour windows load the latest row per entity, and
the size of that set was not measured. Stop if `used_memory` plus the expected load approaches the
cap.

### E2: C2 Flink dual-store

| Item | Detail |
|---|---|
| Decision needed | Host Redpanda and Flink on GKE as these manifests do, or keep them local and connect to cloud Redis and GCS |
| Mutations | Two images; three Deployments; two Services; one ConfigMap; five Jobs; a new registry; objects under `streaming-history/`; keys in Redis `db=1` |
| Resources | With all sinks running: requests 2 CPU and 8 GiB, limits 7 CPU and about 10.3 GiB, on four `e2-standard-4` nodes |
| Not touched | Network policies, IAM, the live registry, Redis `db=0`. Sinks reuse `training` and the labelled Redis path. `ewallet-training` already has `roles/storage.objectUser` |
| Blast radius | Pending pods if capacity is short; shared Redis memory, though this run writes under 100 keys |
| Cost and duration | Existing nodes, unless autoscaling adds one; under 30 minutes |
| Dry run | `block_a_cloud.sh render`, then `kubectl apply --dry-run=server` |
| Verify | Section 3.3 |
| Rollback | Section 3.5 |

The stack was validated with a server dry run on a local kind cluster, in another namespace. It has
never started on GKE. Istio ambient behaviour in `ml-platform` is unverified, and Redpanda or Flink
traffic could need attention there.

### E3: C3 Gold validation

| Item | Detail |
|---|---|
| Mutations | None; it is read-only and writes local files |
| Target | Two Gold run roots in the bucket |
| Cost | About 4.2 GB of GCS egress, under one US dollar at list price |
| Verify | Section 4 |

### E4: optional, new controlled DP2 Gold or DP3 run

Not needed for C3 acceptance, because the existing outputs can be validated. It is listed only in
case the owner wants new Spark UI evidence.

| Item | Detail |
|---|---|
| Mutations | Dataproc Serverless batches; new run-scoped output in GCS |
| Cost | Prior Gold pair used about 68 and 52 DCU-hours |
| Verify | Sections 4 and 6 |
| Rollback | Outputs are run-scoped and can be left unused |

## 8. Rubric status

| Row | IMPLEMENTED | LOCAL_VALIDATED | CLOUD_RUNTIME_VERIFIED | RUBRIC_EVIDENCE_COMPLETE |
|---|---|---|---|---|
| EDAI-2 row 11 | Yes | Yes | No | No |
| EDAI-2 row 12 | Yes | Yes | No | No |
| EDAI-1 rows 14, 25 (Gold gate) | Yes | Yes, on real data read locally | No | No |
| EDAI-1 rows 34, 37 (schema) | Yes | Yes | Not applicable; generated from real logs | No, screenshots pending |
| EDAI-1 row 11 (Spark UI) | Yes | Yes | Not applicable; read from real logs | No, screenshots pending |
| EDAI-1 rows 16, 17 (Flink) | Yes | Yes | No | No |
