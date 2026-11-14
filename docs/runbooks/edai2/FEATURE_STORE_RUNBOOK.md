# Feature Store runbook

This runbook deploys the Feature Store and reproduces its verification on GKE:
batch incremental materialization through Airflow, and a streaming campaign
through Redpanda and Flink. The design is explained in
[FEATURE_STORE.md](../../edai2/mlops/FEATURE_STORE.md). The commands are the ones
used for the verified runs of 2026-11-12 and 2026-11-14, with every run-specific
value turned into a variable.

## Safety classes

Every block is labelled. Run a block only when its class is acceptable.

| Label | Effect |
|---|---|
| `READ-ONLY` | Reads local files, GCS, Kubernetes or Redis; changes nothing |
| `LOCAL` | Creates local files, environments, images or local Airflow/DataHub state; no cloud write |
| `DRY-RUN` | Validates and renders manifests or plans; applies nothing |
| `K8S-MUTATION` | Creates or deletes Kubernetes objects or Secrets |
| `GCS-WRITE` | Creates objects in GCS (create-only; nothing is overwritten or deleted, except the campaign's `registry.db`) |
| `REDIS-WRITE` | Claims a Redis database or writes features to it |
| `COST` | Changes the cluster's node count, and so its cost |

Rules that apply everywhere:

- Never use Redis DB 0 (production) or Secret `feast-online-redis` for
  verification. Never run `FLUSHDB`/`FLUSHALL` and never delete an owner marker.
- Use a new registry ID, DagRun ID and streaming namespace for every run. Do not
  reuse the IDs of earlier runs.
- Never print Secret values, Redis connection strings, tokens or kubeconfig
  contents, and never save them in evidence.
- Images are referenced only by digest and must be built from the commit that
  is checked out.

## 0. Prerequisites

| Item | Requirement |
|---|---|
| Tools | `git`, `docker`, `gcloud`, `kubectl` with the GKE auth plugin, `uv` (one lock per project), `python3` |
| Python environments | `uv sync --locked --project ai_platform/features/feast` (export, retrieval check); `--project data_platform/metadata/datahub` (DataHub publisher); `--project infra/docker/streaming-sinks` (local `check-report`). Airflow 3 in its own environment for the batch DAG |
| GCP | Project `ewallet-data-ai-platform-hg26`, cluster `ewallet-dev` (`asia-southeast1-b`), Artifact Registry repository `ewallet-platform`, data bucket for the dataset (`rehearsal-5gb-v1` → `ewallet-data-ai-platform-hg26-rehearsal-5gb-data`) |
| Kubernetes | Namespace `ml-platform` with KSA `training` (Workload Identity → GSA `ewallet-training`); namespace `feature-store` with the Redis StatefulSet ([`infra/kubernetes/feast-online/`](../../../infra/kubernetes/feast-online)) |
| Redis | `maxmemory-policy noeviction`; one empty database per run, in 2–7 for batch and 8–15 for streaming |
| Capacity | About 9 CPU of shared workloads plus about 2.3 CPU per streaming campaign. The rehearsal cluster used 5 nodes |
| Local RAM | DataHub quickstart needs about 5 GiB. Check `free -g` and start one heavy stack at a time |

### 0.1 Bucket layout

```text
gs://<bucket>/
  features/offline/snapshots/<dataset>/<feature-run>/<snapshot>/      immutable Offline snapshot
      _control/export_manifest.json
      feat_{user,account,device,merchant}_behavior/
  features/registries/<dataset>/online/<registry-id>/registry.db       one Feast registry per run
  features/_iam-probe/<probe-id>.json                                 IAM probe objects
  streaming-features/campaigns/<ns>/runs/<ns>/offline/feat_<view>/    streaming Offline Parquet
  streaming-features/campaigns/<ns>/runs/<ns>/_provenance/<view>/     Kafka offset manifests
  streaming-history/<ns>/                                             accepted-event history
```

### 0.2 IAM on the bucket

Grant `ewallet-training` only conditional bucket bindings (the bucket uses
uniform bucket-level access). Never grant project-wide storage roles,
`storage.admin` or `storage.objectAdmin`.

| Role | Condition (prefixes) |
|---|---|
| `roles/storage.objectViewer` | objects and list prefixes `features/`, `streaming-features/`, `streaming-history/` |
| `roles/storage.objectUser` | `features/registries/<dataset>/online/fs11-*` and `…/online/verify-fs12-*` (Feast overwrites `registry.db` with a generation check, which needs delete) |
| `roles/storage.objectCreator` | `streaming-features/campaigns/verify-fs12-*`, `streaming-history/verify-fs12-*`, `features/_iam-probe/` |

Give every binding an expiry. Registry IDs must therefore start with `fs11-`
and streaming namespaces with `verify-fs12-`.

```bash
# [READ-ONLY] list the bindings and their condition titles
gcloud storage buckets get-iam-policy "gs://$B" --format=json \
  | python3 -c 'import json,sys;[print(b["role"],b.get("condition",{}).get("title")) for b in json.load(sys.stdin)["bindings"]]'
```

## 1. Access, checkout and images

```bash
# [READ-ONLY] cluster credentials in a separate kubeconfig, via the IAM-authenticated DNS endpoint
export KUBECONFIG=$HOME/.cache/ewallet-fs-cloud/kubeconfig
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b \
  --project ewallet-data-ai-platform-hg26 --dns-endpoint
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
kubectl --context "$KUBE_CONTEXT" get namespaces >/dev/null && echo GKE_OK

# [READ-ONLY] a clean checkout of main
cd <repository>
git switch main && git pull --ff-only && test -z "$(git status --short)"
export APPROVED_SHA="$(git rev-parse HEAD)" GIT_SHA="$(git rev-parse HEAD)"
export IMAGE_REPO=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
export EV="$HOME/ewallet-evidence/fs-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$EV"
```

### 1.1 Wake the cluster

```bash
# [READ-ONLY]
python3 ops/gcp/cloud_lab.py --kubeconfig "$KUBECONFIG" status | tee "$EV/gke-before.txt"
# [COST] resize the existing pool; do not use wake-minimal, which scales other Deployments to 0
gcloud container clusters resize ewallet-dev --node-pool cpu-pool --num-nodes 5 \
  --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 --quiet
kubectl --context "$KUBE_CONTEXT" -n feature-store rollout status statefulset/redis --timeout=600s
```

Right after a wake, `kubectl logs`/`exec` can fail with "No agent available"
until the Konnectivity agents are ready. Wait and retry.

### 1.2 Images

Reuse the images if they already exist for `APPROVED_SHA`:

```bash
# [READ-ONLY]
for n in feast-online streaming-sinks flink-block-a; do
  gcloud artifacts docker images describe "$IMAGE_REPO/$n:$APPROVED_SHA" \
    --format='value(image_summary.fully_qualified_digest)'
done | tee "$EV/IMAGES.txt"
```

Otherwise build, probe and push them:

```bash
# [LOCAL] build from the clean checkout and check that the modules import
docker build --build-arg SOURCE_SHA="$APPROVED_SHA" -f ai_platform/inference/Dockerfile -t "$IMAGE_REPO/feast-online:$APPROVED_SHA" .
docker build --build-arg SOURCE_SHA="$APPROVED_SHA" -f infra/docker/streaming-sinks/Dockerfile -t "$IMAGE_REPO/streaming-sinks:$APPROVED_SHA" .
docker build --build-arg SOURCE_SHA="$APPROVED_SHA" -f infra/docker/flink-benchmark/Dockerfile -t "$IMAGE_REPO/flink-block-a:$APPROVED_SHA" .
docker run --rm --entrypoint python "$IMAGE_REPO/feast-online:$APPROVED_SHA" -c \
  'import ai_platform.features.feast.online_operator, ai_platform.features.feast.validation.iam_probe; print("FEAST_OK")'
docker run --rm --entrypoint python "$IMAGE_REPO/streaming-sinks:$APPROVED_SHA" -c \
  'import data_platform.streaming.verification.consistency, data_platform.streaming.feast.service; print("SINKS_OK")'
docker run --rm --entrypoint python "$IMAGE_REPO/flink-block-a:$APPROVED_SHA" -c \
  'import data_platform.streaming.flink.job; print("FLINK_OK")'
# [GCS-WRITE: Artifact Registry] push the immutable tags, then resolve the digests as above
for n in feast-online streaming-sinks flink-block-a; do docker push "$IMAGE_REPO/$n:$APPROVED_SHA"; done
```

```bash
# [READ-ONLY] export digest references only
export FEATURE_IMAGE="$(sed -n 1p "$EV/IMAGES.txt")" SINKS_IMAGE="$(sed -n 2p "$EV/IMAGES.txt")" FLINK_IMAGE="$(sed -n 3p "$EV/IMAGES.txt")"
export FEATURE_IMAGE_SOURCE_SHA=$APPROVED_SHA SINKS_IMAGE_SOURCE_SHA=$APPROVED_SHA FLINK_IMAGE_SOURCE_SHA=$APPROVED_SHA
source scripts/mlops/lib/common.sh && require_digest_pinned_images FEATURE_IMAGE SINKS_IMAGE FLINK_IMAGE
```

### 1.3 Dataset and snapshot

```bash
export DATASET_VERSION=rehearsal-5gb-v1
export B=ewallet-data-ai-platform-hg26-rehearsal-5gb-data          # the only bucket allowed for this dataset
export FEATURE_RUN_ID=<DP3 feature run, e.g. ab-20261110225456-optimized-feat-r261110225459>
export SNAPSHOT_ID=<Feast snapshot, e.g. feast-r5gb-20261111-214544>
export SNAPSHOT_PREFIX="features/offline/snapshots/$DATASET_VERSION/$FEATURE_RUN_ID/$SNAPSHOT_ID"
export MANIFEST_SHA="$(gcloud storage cat "gs://$B/$SNAPSHOT_PREFIX/_control/export_manifest.json" | sha256sum | cut -d' ' -f1)"
```

The values above are the snapshot of the verified runs. A new snapshot comes
from the export in
[`export_gold_to_parquet.py`](../../../ai_platform/features/feast/sources/export_gold_to_parquet.py),
which writes a new snapshot ID and never overwrites an existing one ([LOCAL]
plus [GCS-WRITE]). Check any snapshot before using it:

```bash
# [READ-ONLY] tables present, and Feast point-in-time retrieval matches the Parquet rows
#             (writes only the local --output file; the verified run used --as-of 2026-05-20T12:00:00.5Z)
gcloud storage ls "gs://$B/$SNAPSHOT_PREFIX/"
uv run --locked --project ai_platform/features/feast python -m ai_platform.features.feast.validation.snapshot_retrieval \
  --snapshot-root "gs://$B/$SNAPSHOT_PREFIX" --expected-manifest-sha256 "$MANIFEST_SHA" \
  --as-of <UTC instant inside the snapshot> --sample-size 5 --output "$EV/historical_retrieval.json"
```

### 1.4 Prove the Jobs' permissions

```bash
# [DRY-RUN] render the probe Job
FEAST_ONLINE_IMAGE="$FEATURE_IMAGE" PROBE_BUCKET="$B" PROBE_SNAPSHOT_PREFIX="$SNAPSHOT_PREFIX" \
  PROBE_EXPECTED_MANIFEST_SHA256="$MANIFEST_SHA" scripts/mlops/feature_store_iam_probe.sh "fs-iam-probe-$(date -u +%Y%m%d%H%M%S)"
# [K8S-MUTATION + GCS-WRITE] run it: creates one probe object; expect "status": "PASS"
FEAST_ONLINE_IMAGE="$FEATURE_IMAGE" PROBE_BUCKET="$B" PROBE_SNAPSHOT_PREFIX="$SNAPSHOT_PREFIX" \
  PROBE_EXPECTED_MANIFEST_SHA256="$MANIFEST_SHA" scripts/mlops/feature_store_iam_probe.sh "fs-iam-probe-$(date -u +%Y%m%d%H%M%S)" --execute \
  | tee "$EV/iam-probe.json"
```

The probe passes only if the manifest reads with the right digest, the snapshot
lists, a create in `features/_iam-probe/` succeeds once and is then refused
(412), and writes to `lakehouse/` or `features/offline/` and a list of
`lakehouse/` are refused (403).

## 2. Redis: pick empty databases and point the verification Secrets at them

```bash
# [READ-ONLY] size and owner marker of every database, and memory
R() { kubectl --context "$KUBE_CONTEXT" -n feature-store exec statefulset/redis -c redis -- \
      sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" exec redis-cli --no-auth-warning -n "$0" "$@"' "$@"; }
for db in $(seq 0 15); do echo "db $db size $(R $db DBSIZE) owner $(R $db EXISTS __ewallet_feature_store_campaign_owner_v1__)"; done | tee "$EV/redis-db-scan.txt"
R 0 INFO memory | grep -E '^used_memory:|^maxmemory:'
```

Choose `ROW11_DB` in 2–7 and `ROW12_DB` in 8–15 with size 0 and no owner.

```bash
# [K8S-MUTATION] repoint the two verification-only Secrets without printing the connection string
export ROW11_DB=<empty db in 2-7> ROW12_DB=<empty db in 8-15>
for pair in "feast-online-redis-rehearsal:$ROW11_DB" "feast-online-redis-stream:$ROW12_DB"; do
  name=${pair%%:*} db=${pair#*:}
  kubectl --context "$KUBE_CONTEXT" -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' \
    | base64 -d | sed -E 's/,db=[0-9]+//; s/$/,db='"$db"'/' \
    | kubectl --context "$KUBE_CONTEXT" -n ml-platform create secret generic "$name" \
        --from-file=connection_string=/dev/stdin --dry-run=client -o yaml \
    | kubectl --context "$KUBE_CONTEXT" -n ml-platform apply -f - >/dev/null
  kubectl --context "$KUBE_CONTEXT" -n ml-platform label secret "$name" app.kubernetes.io/part-of=ewallet-fs-verify --overwrite
done
```

### 2.1 Redis restart and ownership

The owner marker includes a fingerprint of the Redis server's `run_id`. After a
Redis restart the fingerprint changes, and every run against the old database
stops. This is intended. Do not rewrite the marker. Choose a new empty database
and a new registry or campaign ID. If persistence, data or ownership is
uncertain, use a dedicated Redis instance.

## 3. Batch incremental materialization

### 3.1 Bootstrap a new registry

The DAG refuses a FeatureView that has never been materialized. A new registry
therefore needs one explicit base load. The base load is setup, not
incremental evidence; keep its output separate.

```bash
export FS11_ID="fs11-r5gb-$(date -u +%Y%m%d%H%M)"
export FEAST_OFFLINE_DATA_ROOT="gs://$B/$SNAPSHOT_PREFIX"
export ONLINE_REGISTRY_URI="gs://$B/features/registries/$DATASET_VERSION/online/$FS11_ID/registry.db"
export FEAST_ONLINE_IMAGE="$FEATURE_IMAGE" ONLINE_STORE_ID=rehearsal-online EXECUTION_POLICY=verification
export FEAST_REDIS_SECRET=feast-online-redis-rehearsal REDIS_CAMPAIGN_ID="$FS11_ID"
export ESTIMATED_REDIS_BYTES=<entities in base + incremental windows × 512>   # 120000000 for the verified windows
export EWALLET_C1_EVIDENCE_ROOT="$EV/row11" O="$EV/row11/bootstrap"; mkdir -p "$O"
J=scripts/mlops/feast_online_job.sh
# [DRY-RUN] without --execute, every call below only prints its Job
# [REDIS-WRITE] claim the database (CLAIMED, or OWNED on a retry)
$J "fs11-claim-$(date -u +%H%M%S)" --execute -- claim | tee "$O/01-redis-claim.json"
# [GCS-WRITE] create the registry with the four FeatureViews
$J "fs11-apply-$(date -u +%H%M%S)" --execute -- apply | tee "$O/02-apply.json"
# [READ-ONLY Job] each view's source time range and watermark
$J "fs11-state0-$(date -u +%H%M%S)" --execute -- state | tee "$O/03-state-before.json"
```

Choose `BASE_START < BASE_END` inside the time range of all four views, and
leave genuine rows after `BASE_END` for the incremental run. For the 5 GB
snapshot the verified run used `[2026-05-31T12:00Z, 18:00Z)` as the base and
`18:00Z → 23:00Z` as the incremental window.

```bash
export BASE_START=<UTC> BASE_END=<UTC>
# [REDIS-WRITE + GCS-WRITE] base load, then state and readback
$J "fs11-base-$(date -u +%H%M%S)" --execute -- materialize --start "$BASE_START" --end "$BASE_END" | tee "$O/04-base-materialize.json"
$J "fs11-state1-$(date -u +%H%M%S)" --execute -- state | tee "$O/05-state-after.json"
$J "fs11-base-read-$(date -u +%H%M%S)" --execute -- parity --cutover "$BASE_END" \
  --sample-window-start "$BASE_START" --sample-size 5 | tee "$O/06-base-readback.json"
```

Continue only if the claim is `CLAIMED`/`OWNED`, `02` lists four views, every
`materialized_end` in `05` equals `BASE_END`, and `06` is `PASS` with non-zero
comparisons.

### 3.2 Start DataHub and the isolated Airflow

```bash
# [LOCAL] DataHub quickstart (about 5 GiB RAM; check free -g first)
DATAHUB_PYTHON="$HOME/miniconda3/envs/datahub/bin/python" DATAHUB_PULL_IMAGES=false DATAHUB_MAPPED_GMS_PORT=18080 \
  ./data_platform/metadata/datahub/runtime/start_datahub.sh start
curl -fsS http://127.0.0.1:18080/health && export DATAHUB_GMS_URL=http://127.0.0.1:18080
# [LOCAL] Airflow 3 with its own AIRFLOW_HOME, database and port 8094
A=scripts/mlops/fs11_isolated_airflow.sh
$A start && $A airflow dags list-import-errors && $A airflow dags unpause feast_incremental_materialization
```

The Airflow tasks inherit this shell's `KUBECONFIG`, `KUBE_CONTEXT`,
`EWALLET_C1_EVIDENCE_ROOT` and `DATAHUB_GMS_URL`. If DataHub requires a token,
export `DATAHUB_GMS_TOKEN` in the shell; never put it in DagRun parameters.

### 3.3 Dry run, then execute

```bash
# [LOCAL] the request; target_end is the end of the incremental window
python3 - "$EV/fs11-dry.json" <<'PY'
import json, os, sys
json.dump({"mode": "dry_run", "approval": "", "data_bucket": os.environ["B"],
  "dataset_version": os.environ["DATASET_VERSION"], "feature_run_id": os.environ["FEATURE_RUN_ID"],
  "feast_snapshot_id": os.environ["SNAPSHOT_ID"], "online_registry_id": os.environ["FS11_ID"],
  "online_store_id": "rehearsal-online", "estimated_redis_bytes": int(os.environ["ESTIMATED_REDIS_BYTES"]),
  "target_end": os.environ["TARGET_END"], "streaming_cutover": "", "max_window_hours": 6, "sample_size": 5,
  "expected_manifest_sha256": os.environ["MANIFEST_SHA"], "feast_online_image": os.environ["FEATURE_IMAGE"],
  "feast_redis_secret": "feast-online-redis-rehearsal", "git_sha": os.environ["APPROVED_SHA"]},
  open(sys.argv[1], "x"), indent=2)
PY
wait_run() { until s=$($A airflow dags state feast_incremental_materialization "$1" 2>/dev/null | tail -1 | cut -c1-7); [[ $s == success || $s == failed ]]; do sleep 20; done; echo "$1 $s"; }

# [DRY-RUN] eight tasks; renders Jobs and DataHub records, changes nothing
export FS11_DRY="manual__fs11-dry-$(date -u +%Y%m%dT%H%M%SZ)"
$A airflow dags trigger feast_incremental_materialization --run-id "$FS11_DRY" --conf "$(tr -d '\n' < "$EV/fs11-dry.json")"
wait_run "$FS11_DRY"

# [LOCAL] the execute request differs only in mode and the dry run's approval token
TOKEN=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["approval_token_for_this_window"])' "$EWALLET_C1_EVIDENCE_ROOT/$FS11_DRY/01-config.json")
python3 -c 'import json,sys;c=json.load(open(sys.argv[1]));c.update(mode="execute",approval=sys.argv[3]);json.dump(c,open(sys.argv[2],"x"),indent=2)' \
  "$EV/fs11-dry.json" "$EV/fs11-exec.json" "$TOKEN"

# [K8S-MUTATION + REDIS-WRITE + GCS-WRITE + DataHub write] the incremental run (about 15 minutes)
export FS11_RUN="manual__fs11-$(date -u +%Y%m%dT%H%M%SZ)"
$A airflow dags trigger feast_incremental_materialization --run-id "$FS11_RUN" --conf "$(tr -d '\n' < "$EV/fs11-exec.json")"
wait_run "$FS11_RUN"
RUN_DIR="$EWALLET_C1_EVIDENCE_ROOT/$FS11_RUN"
sha256sum "$RUN_DIR"/*.json > "$RUN_DIR/SHA256SUMS"; echo "$APPROVED_SHA" > "$RUN_DIR/GIT_SHA"
```

### 3.4 Check the result

```bash
# [READ-ONLY]
python3 - "$RUN_DIR" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1]); load = lambda n: json.loads((d / n).read_text())
w, m, r, h = load("03-window.json"), load("04-materialize.json"), load("06-readback.json"), load("08-datahub-lineage.json")
print("decision", w["decision"]["state"], w["decision"]["start"], "->", w["decision"]["end"])
print("job", m["job_name"], m["evidence"]["registry_generation_before"], "->", m["evidence"]["registry_generation_after"])
print("readback", r["status"], r["evidence"]["comparisons"], "comparisons", r["evidence"]["mismatch_count"], "mismatches")
print("datahub", h.get("verification_status"))
PY
```

Accept the run only if the DagRun is `success` and the decision is `NEW_WINDOW`
(or a `PARTIAL` that loads only the views that need it). The registry generation
must change, readback must show non-zero comparisons and 0 mismatches, and
DataHub must report `PASS`. `COMPLETED` and `NO_NEW_DATA` are valid no-ops, not
incremental loads.

To view the result:

- Airflow at http://127.0.0.1:8094: DAG `feast_incremental_materialization`,
  then the run, then the Graph view.
- DataHub at http://localhost:9002: search `materialize_window`, then open the
  Lineage tab.

If DataHub shows no lineage although the task passed, check the OpenSearch disk.
Above the 95 % flood-stage watermark the indices turn read-only and index writes
are lost. Free disk, remove the read-only block, then restore the indices of only
the affected URNs through `POST /openapi/operations/elasticSearch/restoreIndices`.
Never run a full reindex with `clean`.

## 4. Streaming campaign

```bash
# [READ-ONLY] campaign variables; every path derives from the namespace
export BLOCK_A_NAMESPACE="verify-fs12-$(date -u +%Y%m%d%H%M%S)"
export APPROVED_DATA_BUCKET=$B OFFLINE_FEATURE_ROOT="gs://$B/streaming-features"
export FEAST_OFFLINE_DATA_ROOT="$OFFLINE_FEATURE_ROOT/campaigns/$BLOCK_A_NAMESPACE/runs/$BLOCK_A_NAMESPACE/offline"
export HISTORY_ROOT="gs://$B/streaming-history/$BLOCK_A_NAMESPACE"
export ONLINE_REGISTRY_URI="gs://$B/features/registries/$DATASET_VERSION/online/$BLOCK_A_NAMESPACE/registry.db"
export ONLINE_STORE_ID=rehearsal-stream FEAST_REDIS_SECRET=feast-online-redis-stream
export FEAST_SNAPSHOT_ID=$SNAPSHOT_ID BLOCK_A_ESTIMATED_REDIS_BYTES=10000000 BLOCK_A_EVIDENCE_ROOT="$EV/row12"
unset FEAST_ONLINE_IMAGE REDIS_CAMPAIGN_ID ESTIMATED_REDIS_BYTES
S=scripts/streaming/block_a_cloud.sh
```

```bash
# [DRY-RUN] render the stack and Jobs, and print what each step would do
$S render "$BLOCK_A_NAMESPACE" > "$EV/row12-render.yaml"
$S preflight "$BLOCK_A_NAMESPACE"
$S deploy "$BLOCK_A_NAMESPACE"
$S run "$BLOCK_A_NAMESPACE"
```

Check the render before executing. It must contain the Redpanda and Flink
Deployments and the six Jobs, with every image as a digest, labels for exactly
this namespace, and the derived Offline root.

```bash
# [REDIS-WRITE] claim the campaign database; expect preflight=PASS claim=CLAIMED
$S preflight "$BLOCK_A_NAMESPACE" --execute --approve-redis-claim
# [GCS-WRITE] the campaign's registry (create-only; refuses an existing registry)
FEAST_ONLINE_IMAGE="$SINKS_IMAGE" EXECUTION_POLICY=verification REDIS_CAMPAIGN_ID="$BLOCK_A_NAMESPACE" \
  SNAPSHOT_ID="$FEAST_SNAPSHOT_ID" ESTIMATED_REDIS_BYTES="$BLOCK_A_ESTIMATED_REDIS_BYTES" \
  scripts/mlops/feast_online_job.sh "fs12-apply-$(date -u +%H%M%S)" --execute -- apply | tee "$EV/row12-registry-apply.json"
# [K8S-MUTATION] Redpanda and Flink
$S deploy "$BLOCK_A_NAMESPACE" --execute
# [K8S-MUTATION + GCS-WRITE + REDIS-WRITE] topics, Flink job, writers, scenario
$S run "$BLOCK_A_NAMESPACE" --execute
# [K8S-MUTATION] validator Job and evidence collection; exits non-zero unless verification=PASS
$S collect "$BLOCK_A_NAMESPACE" --execute
```

### 4.1 Check the result

```bash
# [READ-ONLY] independent digest check of the report
RUN_DIR="$BLOCK_A_EVIDENCE_ROOT/$BLOCK_A_NAMESPACE"
uv run --locked --project infra/docker/streaming-sinks python -m data_platform.streaming.verification.cli \
  check-report --report "$RUN_DIR/consistency-report.json" | tee "$RUN_DIR/check-report.txt"
sha256sum "$RUN_DIR"/* > "$RUN_DIR/SHA256SUMS"; echo "$APPROVED_SHA" > "$RUN_DIR/GIT_SHA"

# [READ-ONLY] Flink Dashboard at http://127.0.0.1:8081 (Ctrl-C to stop the forward)
kubectl --context "$KUBE_CONTEXT" -n ml-platform port-forward svc/block-a-flink-jobmanager 8081:8081
# [READ-ONLY] Jobs, Offline files and Online keys
kubectl --context "$KUBE_CONTEXT" -n ml-platform get jobs -l app.kubernetes.io/part-of=ewallet-block-a -o wide
gcloud storage ls --recursive "$FEAST_OFFLINE_DATA_ROOT/" | grep '\.parquet$' | awk -F/ '{print $(NF-1)}' | sort | uniq -c
R "$ROW12_DB" DBSIZE
```

Accept the campaign only if `collect` and `check-report` both exit 0 and the
report shows the following:

- `status` is PASS, with `entities_compared` above 0;
- `direct_offline_online_comparison` is PASS, with 0 mismatches;
- `comparison.counts` has `MISMATCH`, `MISSING` and `STALE` all at 0;
- `offline_verification` reports `PIT_VALUES_VERIFIED` for every row retrieved.

On the dashboard, the Flink job must be `RUNNING` with every task running and
no failed checkpoints. Close the port-forward when you are done. If a forward
from an earlier session is still open, find it with
`pgrep -a kubectl | grep port-forward` and stop it by PID.

### 4.2 Clean up the campaign

```bash
# [K8S-MUTATION] deletes only objects labelled with this namespace; GCS data, Redis keys, registry and evidence remain
$S cleanup "$BLOCK_A_NAMESPACE" --execute
```

A failed campaign keeps its objects until this cleanup, so its logs can be
collected first. Never reuse a completed Job as evidence for a new run. Clean
up, then run a new namespace.

## 5. Stop and sleep

```bash
# [LOCAL]
scripts/mlops/fs11_isolated_airflow.sh stop
./data_platform/metadata/datahub/runtime/start_datahub.sh stop
# [READ-ONLY] expect SAFE_TO_SLEEP
python3 ops/gcp/cloud_lab.py --kubeconfig "$KUBECONFIG" plan-sleep
# [COST]
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py --kubeconfig "$KUBECONFIG" sleep --execute --confirm-cluster ewallet-dev
kubectl --context "$KUBE_CONTEXT" get nodes          # expect: No resources found
```

Keep Redis keys, registries, GCS objects, DataHub records and evidence. Redis
loss is repaired by materializing again, never by flushing.

## 6. Local checks without the cluster

These checks need no cloud access and should pass before any cloud run:

```bash
# [LOCAL] Feast, planning, operator, DataHub lineage, IAM probe
uv run --locked --project ai_platform/features/feast --with pytest python -m pytest -q \
  tests/unit/test_feast_incremental_plan.py tests/unit/test_feast_incremental_orchestration.py \
  tests/integration/test_feast_online_operator.py tests/unit/test_feast_snapshot_retrieval.py \
  tests/unit/test_feast_selection_from_dp3.py tests/unit/test_feature_store_iam_probe.py
# [LOCAL] streaming: Flink operators, writers, validator, runner render and safety checks
uv run --locked --project infra/docker/streaming-sinks --with pytest python -m pytest -q tests/streaming
# [LOCAL] the DAG imports and has eight ordered tasks (Airflow environment)
python -m unittest tests.unit.test_feast_materialization_dag
```

`tests/integration/test_feast_online_operator.py` also runs a real Feast
claim, apply, materialization and parity against a disposable loopback Redis.
The test flushes that Redis, so never point it at a shared one. Like the real
store, it must have bounded memory and `noeviction`:

```bash
# [LOCAL]
docker run -d --rm --name fs-test-redis -p 127.0.0.1:16379:6379 redis:7.4.2-alpine \
  redis-server --maxmemory 256mb --maxmemory-policy noeviction
EWALLET_TEST_REDIS_URL=redis://127.0.0.1:16379 uv run --locked --project ai_platform/features/feast \
  --with pytest python -m pytest -q tests/integration/test_feast_online_operator.py
docker stop fs-test-redis
```

The local stack of real Redpanda, Flink, writers, Feast and Redis in Docker
(used for the replay check) is described in the
[C runbook, section 3.1](C_DATA_PLATFORM_RUNBOOK.md#31-local-rehearsal).
