# MLOps, Feast Online and fraud serving runbook

Execution order after a published ML-2 Training Snapshot exists. Every cloud
command uses explicit project, zone, namespace and kube context
`gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev`.

## OWNER_START_HERE — rows 11 and 12 rerun (rehearsal-5gb-v1)

Prepared 2026-11-12 on branch `feature/edai2-feature-store-runtime`. What ran
before this handoff, with raw files in
[`docs/evidence/edai2/feature-store/fs-20261112/`](../../../evidence/edai2/feature-store/fs-20261112/README.md):

| Already done | Result |
|---|---|
| Phase A export of the real DP3 5 GB Gold features | snapshot `feast-r5gb-20261111-214544`, 63,238,144 rows, 31 features, manifest SHA-256 `3aa7e2f2…2bca`, independent verification PASS |
| Genuine `get_historical_features()` on that snapshot | PASS, 155 comparisons, 0 mismatches, 0 ambiguous |
| Row 11 dry-run DagRun on an isolated Airflow 3.3.1 | `success`, 8/8 tasks, rendered Job manifests, DataHub `DRY_RUN` |
| Row 12 on the local stack (real Redpanda, Flink, writers, Feast, Redis) | PASS, 24 entities, 189 comparisons, 0 mismatches, digest VALID; replay PASS and idempotent |

Not done, because GKE could not be reached from the agent: the Row 11
execute-mode DagRun, Redis materialization, DataHub publication and the Row 12
GKE run. Rows 11 and 12 stay open until you run the steps below and capture
the screenshots yourself.

### Step 0 — two owner approvals (blockers found 2026-11-12)

1. **GCS read for the Feature Store Jobs.** The Jobs run as KSA
   `ml-platform/training` → GSA `ewallet-training`, which has no role on the
   rehearsal bucket (only `ewallet-spark` has `roles/storage.objectUser`). Add
   the same role it already has on the canonical bucket:

   ```bash
   gcloud storage buckets add-iam-policy-binding gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data \
     --member=serviceAccount:ewallet-training@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com \
     --role=roles/storage.objectUser
   gcloud storage buckets get-iam-policy gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data \
     --format=json | grep -A3 objectUser          # expect ewallet-spark and ewallet-training
   ```

2. **GKE control-plane access from this machine.** The control plane accepts
   only the `/32` CIDRs in Terraform (`infra/terraform/gcp/modules/gke`). From
   an unlisted IP every `kubectl` call times out. Check, then either add the IP
   through Terraform (no drift) or temporarily with `gcloud` (Terraform drift
   until you revert it or add it to the variable):

   ```bash
   MYIP=$(curl -s https://api.ipify.org); echo "$MYIP"
   gcloud container clusters describe ewallet-dev --zone asia-southeast1-b \
     --format='value(masterAuthorizedNetworksConfig.cidrBlocks[].cidrBlock)'
   # Temporary path; keep the existing CIDRs in the list:
   gcloud container clusters update ewallet-dev --zone asia-southeast1-b \
     --enable-master-authorized-networks \
     --master-authorized-networks "<existing,comma,separated>,$MYIP/32"
   timeout 30 kubectl --context gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev get ns >/dev/null && echo GKE_OK
   ```

### Step 1 — checkout, wake, images

```bash
cd ~/Develop/ewallet-feature-store-runtime
git switch feature/edai2-feature-store-runtime && git pull --ff-only
test -z "$(git status --short)"
export APPROVED_SHA="$(git rev-parse HEAD)" GIT_SHA="$(git rev-parse HEAD)"
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export IMAGE_REPO=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
export RUN_TAG="$(date -u +%Y%m%d)"
export EV="$HOME/ewallet-evidence/fs-owner-$RUN_TAG"; mkdir -p "$EV"
free -g; df -h /                                   # need >= 6 GiB RAM and >= 5 GiB disk free

# Wake Redis plus room for the Jobs and Flink (2 nodes, about $0.46/h).
# wake-minimal first scales application Deployments outside the profile to 0;
# wake-full (4 nodes) is the alternative that restores everything.
M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
python3 ops/gcp/cloud_lab.py status | tee "$EV/gke-before.txt"
python3 ops/gcp/cloud_lab.py wake-minimal --profile feature-store-verify --nodes 2 --manifest $M
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-minimal --profile feature-store-verify \
  --nodes 2 --manifest $M --execute --confirm-cluster ewallet-dev
kubectl --context "$KUBE_CONTEXT" -n feature-store rollout status statefulset/redis --timeout=600s
```

Images: the three tags `$IMAGE_REPO/{feast-online,streaming-sinks,flink-block-a}:$APPROVED_SHA`
were built and pushed from the branch head on 2026-11-12. Check that they exist
for your `APPROVED_SHA`. If any `describe` fails, run section 3.1.1 (build, probe, push).
Then resolve digests:

```bash
for n in feast-online streaming-sinks flink-block-a; do
  gcloud artifacts docker images describe "$IMAGE_REPO/$n:$APPROVED_SHA" \
    --format='value(image_summary.fully_qualified_digest)'
done | tee "$EV/IMAGES.txt"
export FEATURE_IMAGE="$(sed -n 1p "$EV/IMAGES.txt")" SINKS_IMAGE="$(sed -n 2p "$EV/IMAGES.txt")" \
       FLINK_IMAGE="$(sed -n 3p "$EV/IMAGES.txt")"
export FEATURE_IMAGE_SOURCE_SHA=$APPROVED_SHA SINKS_IMAGE_SOURCE_SHA=$APPROVED_SHA FLINK_IMAGE_SOURCE_SHA=$APPROVED_SHA
```

### Step 2 — pick empty Redis DBs and create two new Secrets

Read-only scan. Production is DB 0, `canonical-verify` is DB 1. Row 11 takes
the first DB in 2–7 with `DBSIZE` 0, Row 12 the first in 8–15. Never flush.

```bash
for db in $(seq 0 15); do
  printf 'db %s size ' "$db"
  kubectl --context "$KUBE_CONTEXT" -n feature-store exec statefulset/redis -c redis -- \
    sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" exec redis-cli --no-auth-warning -n "$0" DBSIZE' "$db"
done | tee "$EV/redis-db-scan.txt"
export ROW11_DB=<first empty in 2-7> ROW12_DB=<first empty in 8-15>
```

Create the Secrets from the production connection string without printing it.
These are new objects. If one already exists, inspect its `db=` and reuse it
only when that DB is empty or owned by this campaign; never overwrite it:

```bash
for pair in "feast-online-redis-rehearsal:$ROW11_DB" "feast-online-redis-stream:$ROW12_DB"; do
  name=${pair%%:*} db=${pair#*:}
  kubectl --context "$KUBE_CONTEXT" -n ml-platform get secret "$name" >/dev/null 2>&1 && { echo "$name exists"; continue; }
  kubectl --context "$KUBE_CONTEXT" -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' \
    | base64 -d | sed -E 's/,db=[0-9]+//; s/$/,db='"$db"'/' \
    | kubectl --context "$KUBE_CONTEXT" -n ml-platform create secret generic "$name" --from-file=connection_string=/dev/stdin
done
```

### Step 3 — Row 11 (FS11-00A … FS11-04)

```bash
export FS11_ID="fs11-r5gb-$RUN_TAG"
export B=ewallet-data-ai-platform-hg26-rehearsal-5gb-data
export DATASET_VERSION=rehearsal-5gb-v1 FEATURE_RUN_ID=ab-20261110225456-optimized-feat-r261110225459
export SNAPSHOT_ID=feast-r5gb-20261111-214544
export MANIFEST_SHA=3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca
export FEAST_OFFLINE_DATA_ROOT="gs://$B/features/offline/snapshots/$DATASET_VERSION/$FEATURE_RUN_ID/$SNAPSHOT_ID"
export ONLINE_REGISTRY_URI="gs://$B/features/registries/$DATASET_VERSION/online/$FS11_ID/registry.db"
export FEAST_ONLINE_IMAGE="$FEATURE_IMAGE" ONLINE_STORE_ID=rehearsal-online EXECUTION_POLICY=verification
export FEAST_REDIS_SECRET=feast-online-redis-rehearsal REDIS_CAMPAIGN_ID="$FS11_ID"
export ESTIMATED_REDIS_BYTES=120000000          # base 66,802 + incremental 141,896 entities x 512 B, rounded up
export EWALLET_C1_EVIDENCE_ROOT="$EV/row11"; mkdir -p "$EWALLET_C1_EVIDENCE_ROOT/bootstrap"
J=scripts/mlops/feast_online_job.sh; O="$EWALLET_C1_EVIDENCE_ROOT/bootstrap"
$J "fs11-claim-$(date -u +%H%M%S)"  --execute -- claim  | tee "$O/01-redis-claim.json"     # FS11-00A
$J "fs11-apply-$(date -u +%H%M%S)"  --execute -- apply  | tee "$O/02-apply.json"           # FS11-00B
$J "fs11-state0-$(date -u +%H%M%S)" --execute -- state  | tee "$O/03-state-before.json"
$J "fs11-base-$(date -u +%H%M%S)"   --execute -- materialize \
   --start 2026-05-31T12:00:00+00:00 --end 2026-05-31T18:00:00+00:00 | tee "$O/04-base-materialize.json"
$J "fs11-state1-$(date -u +%H%M%S)" --execute -- state  | tee "$O/05-state-after.json"     # FS11-00C
$J "fs11-base-read-$(date -u +%H%M%S)" --execute -- parity --cutover 2026-05-31T18:00:00+00:00 \
   --sample-window-start 2026-05-31T12:00:00+00:00 --sample-size 5 | tee "$O/06-base-readback.json"  # FS11-00D
```

Stop unless: claim `CLAIMED`/`OWNED`; apply lists 4 views; every view's
`materialized_end` in `05` is `2026-05-31T18:00:00+00:00`; `06` is `PASS`
with comparisons > 0.

DataHub (GMS on 18080, UI on 9002) and the isolated Airflow (UI
http://127.0.0.1:8094; its own `AIRFLOW_HOME` and a DAG folder holding only
this DAG; the 8084 Airflow is not touched). Its tasks inherit this shell's
`EWALLET_C1_EVIDENCE_ROOT`, `KUBE_CONTEXT` and `DATAHUB_*`, so export them
before `start`:

```bash
DATAHUB_PYTHON="$HOME/miniconda3/envs/datahub/bin/python" DATAHUB_PULL_IMAGES=false DATAHUB_MAPPED_GMS_PORT=18080 \
  ./data_platform/metadata/datahub/runtime/start_datahub.sh start
curl -fsS http://127.0.0.1:18080/health
export DATAHUB_GMS_URL=http://127.0.0.1:18080   # plus DATAHUB_GMS_TOKEN only if GMS needs one
A=scripts/mlops/fs11_isolated_airflow.sh
$A start
$A airflow dags list-import-errors; $A airflow tasks list feast_incremental_materialization
$A airflow dags unpause feast_incremental_materialization
python3 - "$EV/fs11-dry.json" <<'PY'
import json, os, sys
json.dump({"mode": "dry_run", "approval": "", "data_bucket": os.environ["B"],
  "dataset_version": os.environ["DATASET_VERSION"], "feature_run_id": os.environ["FEATURE_RUN_ID"],
  "feast_snapshot_id": os.environ["SNAPSHOT_ID"], "online_registry_id": os.environ["FS11_ID"],
  "online_store_id": "rehearsal-online", "estimated_redis_bytes": int(os.environ["ESTIMATED_REDIS_BYTES"]),
  "target_end": "2026-05-31T23:00:00Z", "streaming_cutover": "", "max_window_hours": 6, "sample_size": 5,
  "expected_manifest_sha256": os.environ["MANIFEST_SHA"], "feast_online_image": os.environ["FEATURE_IMAGE"],
  "feast_redis_secret": "feast-online-redis-rehearsal", "git_sha": os.environ["APPROVED_SHA"]},
  open(sys.argv[1], "x"), indent=2)
PY
export FS11_DRY="manual__fs11-dry-$(date -u +%Y%m%dT%H%M%SZ)"
$A airflow dags trigger feast_incremental_materialization --run-id "$FS11_DRY" --conf "$(tr -d '\n' < "$EV/fs11-dry.json")"
$A airflow dags state feast_incremental_materialization "$FS11_DRY"        # repeat until success
TOKEN=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["approval_token_for_this_window"])' \
  "$EWALLET_C1_EVIDENCE_ROOT/$FS11_DRY/01-config.json"); echo "$TOKEN"
python3 - "$EV/fs11-dry.json" "$EV/fs11-exec.json" "$TOKEN" <<'PY'
import json, sys
conf = json.load(open(sys.argv[1])); conf.update(mode="execute", approval=sys.argv[3])
json.dump(conf, open(sys.argv[2], "x"), indent=2)
PY
export FS11_RUN="manual__fs11-$(date -u +%Y%m%dT%H%M%SZ)"
$A airflow dags trigger feast_incremental_materialization --run-id "$FS11_RUN" --conf "$(tr -d '\n' < "$EV/fs11-exec.json")"
$A airflow dags state feast_incremental_materialization "$FS11_RUN"         # expect success, 8 green
RUN_DIR="$EWALLET_C1_EVIDENCE_ROOT/$(printf '%s' "$FS11_RUN" | tr ':+' '___')"
sha256sum "$RUN_DIR"/*.json > "$RUN_DIR/SHA256SUMS"; git rev-parse HEAD > "$RUN_DIR/GIT_SHA"
```

Expected in `RUN_DIR`: `03-window.json` decision `NEW_WINDOW` from
`2026-05-31T18:00:00+00:00` to `2026-05-31T23:00:00+00:00`; `05` all four
`materialized_end` = target; `06-readback.json` comparisons > 0 and 0
mismatches; `07-provenance.json` outcome `NEW_WINDOW` and the digest image;
`08-datahub-lineage.json` `verification_status=PASS`.

### Step 4 — Row 12 on GKE (FS12-01 … FS12-05)

```bash
export BLOCK_A_NAMESPACE="verify-fs12-$(date -u +%Y%m%d%H%M%S)"
export APPROVED_DATA_BUCKET=$B OFFLINE_FEATURE_ROOT="gs://$B/streaming-features"
export FEAST_OFFLINE_DATA_ROOT="$OFFLINE_FEATURE_ROOT/campaigns/$BLOCK_A_NAMESPACE/runs/$BLOCK_A_NAMESPACE/offline"
export HISTORY_ROOT="gs://$B/streaming-history/$BLOCK_A_NAMESPACE"
export ONLINE_REGISTRY_URI="gs://$B/features/registries/$DATASET_VERSION/online/$BLOCK_A_NAMESPACE/registry.db"
export ONLINE_STORE_ID=rehearsal-stream FEAST_REDIS_SECRET=feast-online-redis-stream
export FEAST_SNAPSHOT_ID=$SNAPSHOT_ID BLOCK_A_ESTIMATED_REDIS_BYTES=10000000
export BLOCK_A_EVIDENCE_ROOT="$EV/row12"
S=scripts/streaming/block_a_cloud.sh
$S preflight "$BLOCK_A_NAMESPACE" --execute --approve-redis-claim
FEAST_ONLINE_IMAGE="$SINKS_IMAGE" EXECUTION_POLICY=verification REDIS_CAMPAIGN_ID="$BLOCK_A_NAMESPACE" \
  SNAPSHOT_ID="$FEAST_SNAPSHOT_ID" ESTIMATED_REDIS_BYTES="$BLOCK_A_ESTIMATED_REDIS_BYTES" \
  scripts/mlops/feast_online_job.sh "fs12-apply-$(date -u +%H%M%S)" --execute -- apply
$S deploy "$BLOCK_A_NAMESPACE" --execute
$S run "$BLOCK_A_NAMESPACE" --execute
$S collect "$BLOCK_A_NAMESPACE" --execute
RUN_DIR="$BLOCK_A_EVIDENCE_ROOT/$BLOCK_A_NAMESPACE"
PYTHONPATH=. infra/docker/streaming-sinks/.venv/bin/python -m data_platform.streaming.verification.cli \
  check-report --report "$RUN_DIR/consistency-report.json"
kubectl --context "$KUBE_CONTEXT" -n ml-platform port-forward svc/block-a-flink-jobmanager 8081:8081   # FS12-01, then Ctrl-C
$S cleanup "$BLOCK_A_NAMESPACE" --execute
```

### Step 5 — screenshots (owner only; never generated)

Take each one only after its raw file exists. Name the file `<ID>.png` and put
it in `$EV/screenshots/`.

| ID | Where | Must be visible | Raw file | Invalid if | Caption |
|---|---|---|---|---|---|
| FS11-00A | terminal `python -m json.tool $O/01-redis-claim.json` | `CLAIMED`/`OWNED`, `db` = `$ROW11_DB`, campaign `$FS11_ID` | `bootstrap/01-redis-claim.json` | DB 0/1, other owner | Redis ownership claim for the Row 11 registry |
| FS11-00B | terminal on `02-apply.json` | four views, registry URI with `$FS11_ID` | `bootstrap/02-apply.json` | other registry, <4 views | Fresh registry with four FeatureViews |
| FS11-00C | terminal on `05-state-after.json` | four `materialized_end` = `18:00:00` | `bootstrap/05-state-after.json` | null or mixed ends | Base window recorded in the registry |
| FS11-00D | terminal on `06-base-readback.json` | `PASS`, comparisons > 0, 0 mismatches | `bootstrap/06-base-readback.json` | zero comparisons | Base Redis readback |
| FS11-01 | Airflow Grid http://127.0.0.1:8094, DagRun `$FS11_RUN` | 8 green tasks, run ID, `mode=execute` conf | `RUN_DIR/01`–`08` | dry-run run, a failed or retried-to-green task without logs | Execute-mode incremental DagRun `[18:00, 23:00)` |
| FS11-02 | DataHub UI (port 9002), DataJob `materialize_window` | Lineage: Offline snapshot → job → four Online datasets with `$FS11_ID` | `08-datahub-lineage.json` | `DRY_RUN`, other registry ID | Materialization lineage in DataHub |
| FS11-03 | terminal on `03-window.json` + `06-readback.json` | `NEW_WINDOW`, watermark 18:00 → 23:00, comparisons > 0, 0 mismatches | `03`, `05`, `06` | `COMPLETED`/`NO_NEW_DATA`, zero comparisons | Watermark advance and Redis readback |
| FS11-04 | GitHub, file links at `$APPROVED_SHA` | DAG, operator, launcher at the SHA in `GIT_SHA` | `RUN_DIR/GIT_SHA`, `$EV/IMAGES.txt` | branch-name links, tag-only image | Row 11 code at the run commit |
| FS12-01 | Flink UI http://127.0.0.1:8081 | job RUNNING/FINISHED, job ID as in `flink-jobs.json` | `RUN_DIR/flink-jobs.json` | local compose dashboard | Flink job for `$BLOCK_A_NAMESPACE` |
| FS12-02 | GCS console under `$FEAST_OFFLINE_DATA_ROOT` + report `offline_verification` | Parquet + manifests; `PIT_VALUES_VERIFIED`, `rows_retrieved` > 0 | `consistency-report.json` | missing manifests, zero rows | Offline Parquet written by the stream and read by Feast |
| FS12-03 | terminal on the report's Online section | Feast Online values + exact timestamps, campaign owner | `consistency-report.json` | `redis-cli` values only | Feast Online readback from Redis |
| FS12-04 | terminal: `check-report` output + summary | `PASS`, `entities_compared` > 0, `features_compared` > 0, `mismatch_count` 0 | `consistency-report.json`, `consistency-report.log` | digest mismatch, zero comparisons | Offline/Online consistency PASS |
| FS12-05 | GitHub links at `$APPROVED_SHA` + `IMAGES.txt` | Flink job, writers, validator at the SHA; digests | `RUN_DIR/GIT_SHA`, `$EV/IMAGES.txt` | mutable tag | Row 12 code and images at the run commit |

### Step 6 — stop and sleep (GKE cost cleanup)

```bash
scripts/mlops/fs11_isolated_airflow.sh stop
./data_platform/metadata/datahub/runtime/start_datahub.sh stop
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py status | tee "$EV/gke-after.txt"          # expect cpu-pool 0 nodes
# If you used the temporary gcloud authorized-network path, restore the old CIDR list.
```

Keep Redis keys, registries, GCS objects, DataHub records and evidence. They
are the run's proof. Never flush Redis.

## Python environments (uv, one lock per deployment boundary)

| Boundary | Project | Used by |
|---|---|---|
| Feast offline + ML-2 | `ai_platform/features/feast` | snapshot export/materialize, `ml2-materializer` image |
| F9 training | `ai_platform/ml` | KFP component image `f9-kfp-runtime` |
| KFP compiler | `ai_platform/ml/pipelines` | `compile_pipeline`, `ops/mlops/submit_f9_run.py` |
| DVC operator | `ai_platform/ml/dvc-operator` | DVC 3.63.0 pointer |
| KServe predictor | `ai_platform/serving` | `fraud-detector` model-inside-image |
| Fraud API + Feast Online | `ai_platform/inference` | `fraud-api` image, `online_operator` Jobs |

`uv lock --check --project <p>` and `uv run --locked --no-sync --project <p>`
for every boundary; images run `uv sync --locked --no-dev`.

## 1. ML-2 Training Snapshot

`docs/ml/ml2_training_snapshot_operator_runbook.md` for export. PIT
materialization runs next to the snapshot as a Cloud Run Job
(`ops/gcp/run_ml2_materialization.py`, image `ml2-materializer`), with the
bounded Dask reader; publication and DVC stay local and create-only.

## 2. F9 on KFP + MLflow

`ops/gcp/prepare_f9_immutable_artifacts.sh` builds the image by digest and
recompiles the package. Submit with `ops/mlops/submit_f9_run.py` through
`kubectl port-forward svc/ml-pipeline-ui 8888:80 -n kubeflow`. The winner is
registered with alias `candidate`; `production` is only set by a human:

```bash
python - <<'PY'
from mlflow import MlflowClient
MlflowClient("http://127.0.0.1:5000").set_registered_model_alias(
    "fraud-detector", "production", "<reviewed version>")
PY
```

## 3. Feature Store evidence run (rows 11 and 12)

This section is the owner-operated Step 3 procedure. The implementation commit
only prepared it; no command with `--execute`, no Airflow execute-mode DagRun and
no live DataHub publication was run during Step 2. Replace every `<...>` before
use and retain the raw directory unchanged.

### 3.0 Execution machines and start gate

Operation labels are literal: `READ_ONLY` changes neither local nor cloud
state; `LOCAL_MUTATION` may create local environments, images, Airflow metadata
or evidence files but performs no cloud write; and
`CLOUD_MUTATION_REQUIRES_OWNER_APPROVAL` may change Kubernetes, Redis, GCS,
Artifact Registry or DataHub and must not run during Step 2.

| Machine | Allowed work | Runtime requirement |
|---|---|---|
| Desktop PC | Feature Store development, locked tests, local image builds/import probes and read-only dry-runs; the Redis ownership-claim preflight is a separately authorized cloud mutation | Repository at the reviewed SHA; Docker, `uv`, `gcloud`, `kubectl` and network access as required. The PC currently has no approved Airflow Conda environment. |
| Laptop, while EDAI-1 DP1–DP3 runs | The existing EDAI-1 campaign only | Do not change its checkout, environment, DAGs or processes. |
| Row 11 Airflow host, after DP1–DP3 finishes | The eight-task Row 11 dry-run/execute workflow | An existing compatible Airflow Conda environment, the approved clean repository SHA, digest-pinned Feature image, GCS/GKE/DataHub access and owner Step 3 approval. The practical current choice is the laptop after the EDAI-1 campaign finishes. |

Do not provision Airflow as part of this corrective pass. The owner may instead
prepare the PC later through the existing project setup, but none of the Row 11
commands below may run there until the same runtime gate passes. On the selected
Row 11 Airflow host, set paths from the environment that actually exists:

```bash
export ROW11_REPO='<absolute path to the approved checkout>'
export AIRFLOW_ENV='<absolute path to the compatible existing Airflow Conda environment>'
export APPROVED_SHA='<40-hex owner-approved Feature Store revision>'
cd "$ROW11_REPO"
test -x "$AIRFLOW_ENV/bin/airflow" && test -x "$AIRFLOW_ENV/bin/python"
test "$(git rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(git status --short)"
export AIRFLOW_BIN="$AIRFLOW_ENV/bin/airflow"
export EWALLET_AIRFLOW_PYTHON="$AIRFLOW_ENV/bin/python"
export EWALLET_DATAHUB_UV="$(command -v uv)"
"$AIRFLOW_BIN" version
"$EWALLET_AIRFLOW_PYTHON" -c 'import airflow; print(airflow.__version__)'
```

Start Airflow only after DP1–DP3 has finished, the owner has approved Step 3,
the runtime gate above passes, and the required images, credentials and network
paths below are ready. Readiness on the PC is not runtime verification.

### 3.1 Shared safety rules

- Use a new Airflow DagRun ID and `verify-...` streaming namespace for every run.
- `canonical-100gb-v1` must use bucket `ewallet-data-ai-platform-hg26-data`;
  `rehearsal-5gb-v1` must use
  `ewallet-data-ai-platform-hg26-rehearsal-5gb-data`. Other pairings fail.
- Rehearsal Row 11 uses `online_store_id=rehearsal-online` and Secret
  `feast-online-redis-rehearsal` (one empty DB in 2–7). Rehearsal Row 12 uses
  `rehearsal-stream` and Secret `feast-online-redis-stream` (one empty DB in
  8–15). `feast-online-redis-verify` (DB 1) stays with `canonical-verify`.
  Never substitute `feast-online-redis` or DB 0.
- Redis DB separation alone is not isolation. Verification DB 1 must be empty
  and atomically claimed, or already carry the identical
  `ewallet-redis-campaign-owner-v1` marker for this campaign, dataset, registry,
  source run, snapshot and physical server fingerprint. A nonempty unclaimed
  database or another owner is a hard stop; use a dedicated Redis instance.
  Never flush or overwrite it. DB 0 remains the production binding and cannot
  be claimed by the verification workflow.
- The Airflow DAG fixes `execution_policy=verification` outside DagRun params.
  The operator independently binds that policy to `canonical-verify` or
  `rehearsal-online` and checks the resolved host, port, DB, Secret, dataset,
  server fingerprint and campaign owner. `canonical-online` is production-only.
- The execute-mode approval is the `approval_token_for_this_window` emitted by
  the exact dry run. Credentials remain in Kubernetes Secrets and
  `DATAHUB_GMS_TOKEN`; do not place them in DAG params or evidence.
- `COMPLETED` and `NO_NEW_DATA` are honest no-op outcomes, not evidence of new
  incremental movement.

Set these on the selected Row 11 Airflow host after the start gate:

```bash
export EWALLET_C1_EVIDENCE_ROOT="$HOME/ewallet-evidence/fs11"
export DATAHUB_GMS_URL='http://127.0.0.1:8080'
export DATAHUB_GMS_TOKEN='<secret, only if DataHub requires it>'
export KUBE_CONTEXT='gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev'
export FEATURE_IMAGE='<digest-pinned streaming/Feast image>'
export GIT_SHA="$APPROVED_SHA"
```

Stop if the branch is not the approved branch (`main`, or
`feature/edai2-feature-store-runtime` before merge), `git status --short` is nonempty, the image is
not pinned by digest, `git rev-parse HEAD` differs from the reviewed commit, or
either lock check fails:

```bash
uv lock --check --project ai_platform/features/feast
uv lock --check --project data_platform/metadata/datahub
"$AIRFLOW_BIN" dags list-import-errors
"$AIRFLOW_BIN" dags list | grep -F feast_incremental_materialization
```

The Airflow host needs Application Default Credentials able to read the pinned
GCS snapshot and registry, kubeconfig access to the named GKE context, network
access to the GKE API and DataHub GMS, and a DataHub token only when GMS requires
one. Verify identities with `gcloud auth list`, `gcloud auth
application-default print-access-token >/dev/null`, `kubectl --context
"$KUBE_CONTEXT" auth can-i get jobs -n ml-platform`, and a read-only DataHub
health request. Do not copy tokens, Redis connection strings or kubeconfig
contents into evidence.

### 3.1.1 Build and pin the reviewed images

This is `LOCAL_MUTATION` and must run only from the final owner-approved commit. Building locally is safe;
`docker push` and Artifact Registry changes require separate owner approval.

```bash
export APPROVED_SHA="$(git rev-parse HEAD)"
test -z "$(git status --short)"
export IMAGE_REPO='asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform'
docker build --build-arg SOURCE_SHA="$APPROVED_SHA" -f ai_platform/inference/Dockerfile -t "$IMAGE_REPO/feast-online:$APPROVED_SHA" .
docker build --build-arg SOURCE_SHA="$APPROVED_SHA" -f infra/docker/streaming-sinks/Dockerfile -t "$IMAGE_REPO/streaming-sinks:$APPROVED_SHA" .
docker build --build-arg SOURCE_SHA="$APPROVED_SHA" -f infra/docker/flink-benchmark/Dockerfile -t "$IMAGE_REPO/flink-block-a:$APPROVED_SHA" .
docker run --rm --entrypoint python "$IMAGE_REPO/feast-online:$APPROVED_SHA" -c \
  'import ai_platform.features.feast.online_operator, ai_platform.features.feast.redis_isolation; print("FEAST_SOURCE_OK")'
docker run --rm --entrypoint python "$IMAGE_REPO/streaming-sinks:$APPROVED_SHA" -c \
  'import data_platform.streaming.verification.consistency, data_platform.streaming.feast.service; print("SINK_SOURCE_OK")'
docker run --rm --entrypoint python "$IMAGE_REPO/flink-block-a:$APPROVED_SHA" -c \
  'import data_platform.streaming.flink.job; print("FLINK_SOURCE_OK")'
for image in feast-online streaming-sinks flink-block-a; do
  test "$(docker image inspect "$IMAGE_REPO/$image:$APPROVED_SHA" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$APPROVED_SHA"
done
```

Publishing is `CLOUD_MUTATION_REQUIRES_OWNER_APPROVAL`. After the owner
separately approves publishing, push the three immutable tags,
resolve their registry digests, and export only `name@sha256:...` values:

```bash
docker push "$IMAGE_REPO/feast-online:$APPROVED_SHA"
docker push "$IMAGE_REPO/streaming-sinks:$APPROVED_SHA"
docker push "$IMAGE_REPO/flink-block-a:$APPROVED_SHA"
export FEATURE_IMAGE="$(gcloud artifacts docker images describe "$IMAGE_REPO/feast-online:$APPROVED_SHA" --format='value(image_summary.fully_qualified_digest)')"
export SINKS_IMAGE="$(gcloud artifacts docker images describe "$IMAGE_REPO/streaming-sinks:$APPROVED_SHA" --format='value(image_summary.fully_qualified_digest)')"
export FLINK_IMAGE="$(gcloud artifacts docker images describe "$IMAGE_REPO/flink-block-a:$APPROVED_SHA" --format='value(image_summary.fully_qualified_digest)')"
source scripts/mlops/lib/common.sh
require_digest_pinned_images FEATURE_IMAGE SINKS_IMAGE FLINK_IMAGE
docker pull "$FEATURE_IMAGE"
docker pull "$SINKS_IMAGE"
docker pull "$FLINK_IMAGE"
export FEATURE_IMAGE_SOURCE_SHA="$(docker image inspect "$FEATURE_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
export SINKS_IMAGE_SOURCE_SHA="$(docker image inspect "$SINKS_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
export FLINK_IMAGE_SOURCE_SHA="$(docker image inspect "$FLINK_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
require_matching_source_shas "$APPROVED_SHA" \
  FEATURE_IMAGE_SOURCE_SHA SINKS_IMAGE_SOURCE_SHA FLINK_IMAGE_SOURCE_SHA
```

For every executed Job/Deployment, retain `kubectl ... get ... -o json` and
check both `spec...image` and runtime `status.containerStatuses[].imageID` against
the approved digest. Record `APPROVED_SHA` and all three digests with the raw
campaign artifacts; a mutable tag or an image that fails the import probes is
invalid evidence.

```bash
kubectl --context "$KUBE_CONTEXT" -n ml-platform get pods \
  -l 'app.kubernetes.io/part-of in (ewallet-mlops,ewallet-block-a)' \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{range .spec.containers[*]}spec={.image}{"\n"}{end}{range .status.containerStatuses[*]}runtime={.imageID}{"\n"}{end}{end}' \
  | tee "$HOME/ewallet-evidence/deployed-image-ids.txt"
```

Every relevant `spec=` value must equal one of the three approved digest refs,
and every corresponding `runtime=` value must end in that same `sha256:...`.
Validate the Row 11 operator pod against `FEATURE_IMAGE`, the four Row 12 sink
and validator Jobs against `SINKS_IMAGE`, and the Row 12 Flink pods against
`FLINK_IMAGE`; one valid image never excuses another invalid or empty value.

### 3.1.2 Redis restart and ownership recovery

The Redis fingerprint intentionally includes Redis `run_id`. A restart changes
that value, so the old owner marker no longer authorizes writes even when Redis
persistence restored the marker and data. A fingerprint mismatch is a hard,
fail-closed stop; it is not permission to rewrite the marker.

1. Stop all Feature Store writers. Record only sanitized output from `INFO
   server`, `INFO persistence`, `INFO memory`, the claimed DB's `DBSIZE`, the owner marker,
   Secret name/resourceVersion and the calculated host/port/DB/run fingerprint.
   Inspect the decoded Secret connection only interactively to confirm
   host/port/DB; never print or save credentials.
2. Compare the old and current target: Kubernetes namespace/StatefulSet identity,
   Service DNS name, port, DB, Secret resourceVersion, persistence status and
   restored DB size. Verify that the marker still names the exact campaign,
   dataset, registry URI, logical store, source run and snapshot. Check the
   registry and immutable GCS roots read-only as well.
3. If anything other than `run_id`/derived fingerprint changed, or persistence,
   data or ownership is uncertain, use a new empty isolated Redis target. Never
   run `FLUSHDB`, delete the marker or claim a nonempty database.
4. The current automation deliberately has no ownership-takeover or restart
   rebind command. Reuse is automatically safe only when the fingerprint and
   owner marker are still identical (that is, no restart identity change). After
   a genuine restart, the supported safe path is a new isolated target and new
   campaign/registry. Any future audited compare-and-swap recovery mechanism
   requires separate owner approval and code review before use.

Provisioning/selecting the replacement target and any resumed cloud mutation
each require explicit owner approval. Keep the failed preflight and sanitized
read-only comparison as evidence of the recovery decision; do not manufacture a
passing preflight for the old fingerprint.

### 3.2 Row 11 — incremental materialization and DataHub

Create the JSON once. The example is the rehearsal contract. An independently
approved canonical verification run uses `canonical-verify`, never
`canonical-online`.

```bash
export FS11_CONF="$HOME/ewallet-evidence/fs11-conf.json"
python - "$FS11_CONF" "$FEATURE_IMAGE" "$GIT_SHA" <<'PY'
import json, sys
path, image, sha = sys.argv[1:]
json.dump({
  "mode": "dry_run", "approval": "",
  "data_bucket": "ewallet-data-ai-platform-hg26-rehearsal-5gb-data",
  "dataset_version": "rehearsal-5gb-v1",
  "feature_run_id": "<accepted-feature-run-id>",
  "feast_snapshot_id": "<accepted-snapshot-id>",
  "online_registry_id": "<new-verification-registry-id>",
  "online_store_id": "rehearsal-online",
  "estimated_redis_bytes": <owner-reviewed-positive-byte-estimate>,
  "target_end": "<UTC-exclusive-window-end>",
  "streaming_cutover": "<UTC-cutover-or-empty>",
  "max_window_hours": 6, "sample_size": 5,
  "expected_manifest_sha256": "<64-hex-manifest-digest>",
  "feast_online_image": image,
  "feast_redis_secret": "feast-online-redis-rehearsal",
  "git_sha": sha,
}, open(path, "w"), indent=2)
PY
```

#### Fresh-registry bootstrap (required once, not incremental evidence)

The Airflow planner deliberately rejects any FeatureView whose
`materialized_end` is missing. Before the first incremental DagRun, initialize
the new registry explicitly with the existing operator. These commands are
cloud mutations and require owner approval; keep their JSON output under a
`bootstrap/` directory and never label it FS11-01.

```bash
export ONLINE_REGISTRY_URI='gs://<bucket>/features/registries/<dataset>/online/<new-id>/registry.db'
export FEAST_ONLINE_IMAGE="$FEATURE_IMAGE"
export FEAST_OFFLINE_DATA_ROOT='gs://<bucket>/features/offline/snapshots/<dataset>/<feature-run>/<snapshot-id>'
export ONLINE_STORE_ID='rehearsal-online'
export EXECUTION_POLICY='verification'
export FEAST_REDIS_SECRET='feast-online-redis-rehearsal'
export REDIS_CAMPAIGN_ID='<new-id>'
export DATASET_VERSION='rehearsal-5gb-v1'
export FEATURE_RUN_ID='<accepted-feature-run-id>'
export SNAPSHOT_ID='<accepted-snapshot-id>'
export ESTIMATED_REDIS_BYTES='<same owner-reviewed positive estimate as FS11_CONF>'
mkdir -p "$EWALLET_C1_EVIDENCE_ROOT/bootstrap"

# 1. Atomically claim the isolated Redis target; fails on production,
#    conflicting ownership, or an ambiguous nonempty DB.
scripts/mlops/feast_online_job.sh "fs11-claim-$(date -u +%H%M%S)" --execute -- claim \
  | tee "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/01-redis-claim.json"
# 2. Create the registry and apply exactly four FeatureViews.
scripts/mlops/feast_online_job.sh "fs11-apply-$(date -u +%H%M%S)" --execute -- apply \
  | tee "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/02-apply.json"
# 3. Inspect immutable source min/max values. Choose BASE_START/BASE_END inside
#    all four reported ranges, with BASE_START < BASE_END and genuine rows in
#    every table. BASE_END must precede the intended incremental target_end.
scripts/mlops/feast_online_job.sh "fs11-state0-$(date -u +%H%M%S)" --execute -- state \
  | tee "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/03-state-before.json"
export BASE_START='<UTC instant at or before the first chosen genuine rows>'
export BASE_END='<UTC instant after genuine rows in all four views and before target_end>'
scripts/mlops/feast_online_job.sh "fs11-base-$(date -u +%H%M%S)" --execute -- \
  materialize --start "$BASE_START" --end "$BASE_END" \
  | tee "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/04-base-materialize.json"
# 4. Prove all four registry intervals end at BASE_END and Online readback is real.
scripts/mlops/feast_online_job.sh "fs11-state1-$(date -u +%H%M%S)" --execute -- state \
  | tee "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/05-state-after.json"
scripts/mlops/feast_online_job.sh "fs11-base-read-$(date -u +%H%M%S)" --execute -- \
  parity --cutover "$BASE_END" --sample-window-start "$BASE_START" --sample-size 5 \
  | tee "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/06-base-readback.json"
```

Stop unless the claim is `CLAIMED`/`OWNED`, apply lists all four canonical
views, every post-state `materialized_end` is non-null and equals `BASE_END`, and
readback is `PASS` with nonzero comparisons. The later Airflow `target_end` must
be greater than `BASE_END`, within the snapshot/cutover and contain new genuine
source rows. A base load, `COMPLETED`, or `NO_NEW_DATA` cannot satisfy FS11-01.

1. **Preflight (`LOCAL_MUTATION`; Row 11 Airflow host; FS11-03).** Start/reuse Airflow, verify the DAG
   has eight tasks, and review the bootstrap state/claim artifacts. They must
   name this exact registry, physical fingerprint and owner; do not run the
   Row 12 preflight under a different namespace against the same DB.

   ```bash
   scripts/start_airflow_local.sh
   "$AIRFLOW_BIN" dags list-import-errors
   "$AIRFLOW_BIN" tasks list feast_incremental_materialization
   python -m json.tool "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/01-redis-claim.json"
   python -m json.tool "$EWALLET_C1_EVIDENCE_ROOT/bootstrap/05-state-after.json"
   ```

   Expected: no import errors, exactly eight ordered tasks, claim status
   `CLAIMED`/`OWNED`, matching ownership/fingerprint, and safe current capacity.
   Stop on any mismatch. Save terminal output under
   `$EWALLET_C1_EVIDENCE_ROOT/preflight/`.

2. **Dry-run (`LOCAL_MUTATION`; Row 11 Airflow host; FS11-01/03 readiness).** This reads the immutable manifest
   but renders Kubernetes/DataHub records without applying or publishing them.

   ```bash
   export FS11_DRY_RUN="manual__fs11-dry-$(date -u +%Y%m%dT%H%M%SZ)"
   "$AIRFLOW_BIN" dags trigger feast_incremental_materialization \
     --run-id "$FS11_DRY_RUN" --conf "$(tr -d '\n' < "$FS11_CONF")"
   "$AIRFLOW_BIN" dags state feast_incremental_materialization "$FS11_DRY_RUN"
   find "$EWALLET_C1_EVIDENCE_ROOT" -type f -name '08-datahub-lineage.json' -print
   ```

   Expected: DagRun `success`; `01-config.json` contains the approval token;
   `08-datahub-lineage.json` says `DRY_RUN` and contains current-run URNs.
   Stop if any task mutates Kubernetes/DataHub, an artifact is missing, or an
   identifier is historical/unrelated.

3. **Execute (`CLOUD_MUTATION_REQUIRES_OWNER_APPROVAL`; Row 11 Airflow host; FS11-01–03).**
   Copy the dry-run approval token into a new JSON file, change `mode` to
   `execute`, and use a new DagRun ID. Do not reuse the dry-run artifact folder.

   ```bash
   export FS11_EXEC_CONF="$HOME/ewallet-evidence/fs11-execute-conf.json"
   # Edit only mode and approval; retain the exact reviewed request identities.
   export FS11_RUN="manual__fs11-$(date -u +%Y%m%dT%H%M%SZ)"
   "$AIRFLOW_BIN" dags trigger feast_incremental_materialization \
     --run-id "$FS11_RUN" --conf "$(tr -d '\n' < "$FS11_EXEC_CONF")"
   ```

   Expected: eight green tasks. Stop immediately on failed target fingerprint,
   capacity, manifest, Job completion, nonzero mismatches, DataHub publication
   or semantic readback. Do not retry with relaxed identities.

4. **Verify and collect (`LOCAL_MUTATION` plus cloud reads; Row 11 Airflow host; FS11-01–04).**

   ```bash
   "$AIRFLOW_BIN" dags state feast_incremental_materialization "$FS11_RUN"
   RUN_DIR="$EWALLET_C1_EVIDENCE_ROOT/$(printf '%s' "$FS11_RUN" | tr ':+' '___')"
   python -m json.tool "$RUN_DIR/07-provenance.json"
   python -m json.tool "$RUN_DIR/08-datahub-lineage.json"
   sha256sum "$RUN_DIR"/*.json > "$RUN_DIR/SHA256SUMS"
   git rev-parse HEAD > "$RUN_DIR/GIT_SHA"
   ```

   Require a real `NEW_WINDOW` or `PARTIAL` decision, registry advancement, four
   materialized views, nonzero readback comparisons, zero mismatches, and
   DataHub `verification_status=PASS`. Capture FS11-01 from Airflow Grid with
   DagRun/dataset/window visible; FS11-02 from DataHub showing Offline snapshot →
   materialization DataJob → four Online datasets; FS11-03 from the JSON summary.
   FS11-04 is the commit-pinned code link. Keep raw files in `RUN_DIR`.

5. **Cleanup (`LOCAL_MUTATION`; Row 11 Airflow host).** Stop only the local Airflow runtime. Do not delete the
   registry, DataHub records, evidence, Redis keys or immutable GCS snapshot.

   ```bash
   scripts/start_airflow_local.sh --stop
   ```

### 3.3 Row 12 — streaming Offline and Online integration

Set a unique namespace and paths. The runner resolves the approved bucket from
the dataset registry and rejects every cross-bucket, non-normalized or
campaign-mismatched URI before contacting Kubernetes. `FEAST_OFFLINE_DATA_ROOT`
must be the exact derived path written by the separate offline consumer.

```bash
export BLOCK_A_NAMESPACE="verify-fs12-$(date -u +%Y%m%d%H%M%S | tr '[:upper:]' '[:lower:]')"
export FLINK_IMAGE='<digest-pinned Flink image>'
export SINKS_IMAGE='<digest-pinned streaming sinks image>'
export APPROVED_SHA='<40-hex owner-approved Feature Store revision>'
export FLINK_IMAGE_SOURCE_SHA='<org.opencontainers.image.revision from FLINK_IMAGE>'
export SINKS_IMAGE_SOURCE_SHA='<org.opencontainers.image.revision from SINKS_IMAGE>'
export DATASET_VERSION='<canonical-100gb-v1-or-rehearsal-5gb-v1>'
case "$DATASET_VERSION" in
  canonical-100gb-v1) export APPROVED_DATA_BUCKET='ewallet-data-ai-platform-hg26-data' ;;
  rehearsal-5gb-v1) export APPROVED_DATA_BUCKET='ewallet-data-ai-platform-hg26-rehearsal-5gb-data' ;;
  *) echo "unapproved dataset" >&2; return 1 2>/dev/null || exit 1 ;;
esac
export OFFLINE_FEATURE_ROOT="gs://$APPROVED_DATA_BUCKET/streaming-features"
export FEAST_OFFLINE_DATA_ROOT="$OFFLINE_FEATURE_ROOT/campaigns/$BLOCK_A_NAMESPACE/runs/$BLOCK_A_NAMESPACE/offline"
export HISTORY_ROOT="gs://$APPROVED_DATA_BUCKET/streaming-history/$BLOCK_A_NAMESPACE"
export ONLINE_REGISTRY_URI="gs://$APPROVED_DATA_BUCKET/features/registries/$DATASET_VERSION/online/<new-id>/registry.db"
export FEAST_REDIS_SECRET='<feast-online-redis-verify for canonical, feast-online-redis-stream for rehearsal>'
export ONLINE_STORE_ID='<canonical-verify for canonical, rehearsal-stream for rehearsal>'
export FEATURE_RUN_ID='<accepted-feature-or-streaming-source-run-id>'
export FEAST_SNAPSHOT_ID='<accepted-snapshot-id>'
export BLOCK_A_ESTIMATED_REDIS_BYTES='<owner-reviewed-positive-byte-estimate>'
export BLOCK_A_EVIDENCE_ROOT="$HOME/ewallet-evidence/fs12"
```

1. **Preflight and dry-run (`LOCAL_MUTATION`; PC; FS12 readiness).** These
   commands validate and render only; the redirect creates a local evidence
   file. They do not inspect or claim Redis and do not write Kubernetes objects.

   ```bash
   scripts/streaming/block_a_cloud.sh preflight "$BLOCK_A_NAMESPACE"
   scripts/streaming/block_a_cloud.sh render "$BLOCK_A_NAMESPACE" \
     > "$HOME/ewallet-evidence/fs12-render.yaml"
   scripts/streaming/block_a_cloud.sh deploy "$BLOCK_A_NAMESPACE"
   scripts/streaming/block_a_cloud.sh run "$BLOCK_A_NAMESPACE"
   scripts/streaming/block_a_cloud.sh collect "$BLOCK_A_NAMESPACE"
   scripts/streaming/block_a_cloud.sh cleanup "$BLOCK_A_NAMESPACE"
   ```

   Expected: commands/plans only, no applied objects; render includes
   `block-a-offline`, bounded resources, ownership labels for exactly
   `$BLOCK_A_NAMESPACE` and the exact derived offline root. `render` requires
   that positional namespace, performs no Kubernetes call or Redis claim and
   creates no preflight artifact. Stop if the writer, validator flags, digest
   pins, namespace or paths differ.

2. **Execute (`CLOUD_MUTATION_REQUIRES_OWNER_APPROVAL`; PC; FS12-01–04).**
   The first command atomically claims Redis ownership, so `--execute` alone is
   deliberately insufficient. Fixed Block A object names implement a strict
   single-active-campaign lifecycle: absent objects or an identical campaign
   retry are accepted; foreign or unlabelled objects are a hard stop and require
   owner-controlled matching-campaign cleanup first. Jobs are never reused as
   fresh evidence, even for the same campaign: collect their failure logs, then
   run matching-campaign cleanup before retrying.

   ```bash
   scripts/streaming/block_a_cloud.sh preflight "$BLOCK_A_NAMESPACE" \
     --execute --approve-redis-claim
   # Initialize this new streaming registry after the ownership claim. This is
   # setup, not FS12 evidence; it is create-only and refuses registry reuse.
   FEAST_ONLINE_IMAGE="$SINKS_IMAGE" ONLINE_STORE_ID="$ONLINE_STORE_ID" \
     EXECUTION_POLICY=verification \
     REDIS_CAMPAIGN_ID="$BLOCK_A_NAMESPACE" SNAPSHOT_ID="$FEAST_SNAPSHOT_ID" \
     ESTIMATED_REDIS_BYTES="$BLOCK_A_ESTIMATED_REDIS_BYTES" \
     scripts/mlops/feast_online_job.sh "fs12-apply-$(date -u +%H%M%S)" --execute -- apply
   scripts/streaming/block_a_cloud.sh deploy "$BLOCK_A_NAMESPACE" --execute
   scripts/streaming/block_a_cloud.sh run "$BLOCK_A_NAMESPACE" --execute
   scripts/streaming/block_a_cloud.sh collect "$BLOCK_A_NAMESPACE" --execute
   ```

   Expected: healthy Flink run plus completed `block-a-history`,
   `block-a-offline`, `block-a-feast`, and publish Jobs. The offline writer is a
   separate consumer/Job, not a fabricated Flink sink. Its create-only Parquet
   and offset manifests make replay idempotent; the path is verification-only
   and pod-local Flink checkpoints are not production-durable recovery.

3. **Verify, screenshots and collect (`CLOUD_MUTATION_REQUIRES_OWNER_APPROVAL`;
   PC; FS12-01–05).** The collect command creates the validator Job, which calls
   real `FeatureStore.get_historical_features`, reads Feast Online, preserves
   source-event recomputation, requires nonzero direct comparisons and exits
   nonzero on a mismatch.

   ```bash
   RUN_DIR="$BLOCK_A_EVIDENCE_ROOT/$BLOCK_A_NAMESPACE"
   test -s "$RUN_DIR/consistency-report.log"
   python -m data_platform.streaming.verification.cli check-report \
     --report "$RUN_DIR/consistency-report.json"
   sha256sum "$RUN_DIR"/* > "$RUN_DIR/SHA256SUMS"
   git rev-parse HEAD > "$RUN_DIR/GIT_SHA"
   ```

   Require source-event, persisted Offline, Online readback and direct
   Offline/Online checks all `PASS`, comparisons > 0 and mismatches = 0. Capture
   `check-report` also recomputes the canonical SHA-256 over every report field
   except `report_digest` and requires an exact `sha256:` plus 64 lowercase-hex
   match; a prefix-only, truncated, malformed or content-mismatched digest is a
   failure. A failed/timed-out validator Job, missing/malformed/tampered report,
   semantic failure, zero comparison or nonzero mismatch makes `collect` exit
   nonzero even when log collection succeeds. The raw validator log, extracted
   report and other available failure artifacts remain in `RUN_DIR` for review.
   Require a zero exit from both `collect` and the independent `check-report`.
   Capture
   FS12-01 from Flink Dashboard; FS12-02 from GCS plus genuine Feast historical
   query output; FS12-03 from Online readback; FS12-04 from the consistency
   report. FS12-05 is the commit-pinned code link. Raw evidence is `RUN_DIR`.
   Capture screenshots only after the execute Jobs have completed and the raw
   report has been written: Flink Dashboard for FS12-01, immutable GCS objects
   plus the genuine historical result for FS12-02, Online readback for FS12-03,
   and the final consistency report for FS12-04. A dry-run screen is readiness,
   not runtime evidence.

4. **Cleanup (`CLOUD_MUTATION_REQUIRES_OWNER_APPROVAL`; PC; after collection).**

   ```bash
   scripts/streaming/block_a_cloud.sh cleanup "$BLOCK_A_NAMESPACE" --execute
   ```

   Expected: ownership of every fixed-name object is inspected first; only
   objects carrying both `app.kubernetes.io/part-of=ewallet-block-a`
   and `ewallet.block-a/namespace=$BLOCK_A_NAMESPACE` are deleted. Missing,
   stale or mismatched preflight artifacts, foreign objects or missing ownership
   labels block cleanup. A failed/interrupted campaign retains its objects and
   evidence until this matching-campaign cleanup is explicitly approved. Stop if
   the selector is broader. Redis, registries, GCS feature/history objects and
   evidence remain.

### 3.4 Evidence acceptance matrix

| ID | Owner-operated command/UI | Raw artifact and required observation | Invalid evidence |
|---|---|---|---|
| FS11-01 | Airflow Grid for the execute DagRun; `airflow dags state ...` | `01`–`08` JSON files and Airflow logs; eight green tasks, `NEW_WINDOW`/truthful `PARTIAL`, registry advancement | Dry-run, bootstrap/base load, no-op, failed/skipped movement, mutable image |
| FS11-02 | DataHub UI on the stable `materialize_window` DataJob | `08-datahub-lineage.json`; exact Offline→job→affected Online FeatureView edges and semantic readback | Window-specific dataset URNs, four edges for a partial run, screenshot without raw readback |
| FS11-03 | `online_operator state` and `parity` via the operator Job | State/readback JSON; exact dataset/registry/store owner, nonzero comparisons, zero mismatch | DB number alone, missing owner marker, wrong fingerprint, null registry state |
| FS11-04 | Commit-pinned repository links for the reviewed SHA | `GIT_SHA`, image digest records and links containing that SHA | Local/uncommitted or branch-only links, tag-only image identity |
| FS12-01 | `kubectl port-forward ... 8081:8081`, Flink Dashboard | `flink-jobs.json`, JobID and UI showing the namespace run | Local/synthetic dashboard, unrelated namespace, no raw REST state |
| FS12-02 | GCS object view plus validator's real Feast historical query | Immutable Parquet rows/manifests and consistency report with the authoritative latest-eligible persisted URI/timestamp; `PIT_VALUES_VERIFIED` for every row and no identical-value ambiguity | Query timestamp presented as source timestamp, exact-row identity inferred only from equal values, fabricated rows, missing manifests |
| FS12-03 | Feast Online readback performed by validator | Raw consistency report with Online timestamps and campaign owner identity | Redis CLI values without Feast readback, wrong registry/store/campaign |
| FS12-04 | `block_a_cloud.sh collect "$BLOCK_A_NAMESPACE" --execute` | Create-only report with source, Offline, Online and direct comparison PASS; comparisons > 0 | Newer Online row silently compared to stale Offline, rounded timestamps, zero comparisons |
| FS12-05 | Commit-pinned repository links for the reviewed SHA | `GIT_SHA`, `FLINK_IMAGE`/`SINKS_IMAGE` digests and code links | Mutable tags, wrong source SHA, missing deployed `imageID` |

Every artifact must name the same dataset and Row 11 registry ID or Row 12
namespace. Screenshot only the genuine UI after retaining the raw artifact; a
screenshot without the listed raw proof does not close the item.

Redis loss is recovered by replay/materialization; never flush Redis as a
rollback step.

## 4. KServe Standard + Fraud API

```bash
scripts/mlops/install_kserve.sh --execute          # cert-manager + KServe 0.16.0, Standard, no ingress
python -m ops.mlops.bake_serving_image --alias candidate --execute ...
kubectl apply -f infra/kubernetes/fraud-serving/namespace.yaml -f infra/kubernetes/fraud-serving/network-policy.yaml
envsubst < infra/kubernetes/fraud-serving/inferenceservice.yaml.tmpl | kubectl apply -f -
envsubst < infra/kubernetes/fraud-serving/fraud-api.yaml.tmpl | kubectl apply -f -
```

Wire contract `ewallet-fraud-v2-wire-v1` (`ai_platform/inference/wire.py`):
41 named V2 inputs in training order, JSON null for missing values, response
`model_version` must equal the deployed version.

Rollback: re-apply the previous InferenceService image digest and set
`EWALLET_MODEL_VERSION` back; Redis is re-materialized, never flushed as a
rollback step.

## 5. Cleanup

Scale `cpu-pool` back to 0 after evidence collection. Durable GCS objects,
registries, PostgreSQL and MLflow artifacts are never deleted.
