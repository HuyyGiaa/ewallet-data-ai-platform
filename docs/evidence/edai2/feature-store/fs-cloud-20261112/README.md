# Feature Store rows 11 and 12 — GKE/GCS runtime of 2026-11-12

This was the cloud run of `rehearsal-5gb-v1`. The code was commit `e913224f`.
All three images are pinned by digest and were built from that commit:

- `feast-online@sha256:30b23392…f3c5`
- `streaming-sinks@sha256:34a7835f…f878`
- `flink-block-a@sha256:97d4f07f…3bc8`

The snapshot export, the historical retrieval and the local Row 12 run are in
[`../fs-20261112/`](../fs-20261112/README.md). Large logs (`*.log`, ignored by
git) stay in `~/ewallet-evidence/fs-cloud-20261112/`. No screenshot was
generated. The streaming check was repeated on 2026-11-14 with the
images built from `6e3a81b1`; see [`../fs12-cloud-20261114/`](../fs12-cloud-20261114/README.md).
Both runs are described in [FEATURE_STORE.md](../../../../edai2/mlops/FEATURE_STORE.md).

## Access (`network/`, `iam/`)

- **Control plane.** Authorized networks were not changed; they are still
  three `/32` CIDRs (`control-plane-before.json`). `kubectl` reached the cluster
  through the cluster's DNS-based endpoint, which authenticates with IAM. A
  separate kubeconfig was used (`kubectl-get-ns.txt`). There was nothing to
  roll back.
- **GCS IAM.** Before the run, `ewallet-training` had no role on the rehearsal
  bucket and none at project level (`bucket-iam-before.json`,
  `project-roles-training.txt`). Three conditional bucket bindings were added,
  each expiring 2026-11-27 (`bucket-iam-after.json`, `conditions.txt`):

  | Role | Scope |
  |---|---|
  | `objectViewer` | objects and list prefixes `features/`, `streaming-features/`, `streaming-history/` |
  | `objectUser` | `features/registries/rehearsal-5gb-v1/online/fs11-*` and `…/verify-fs12-*`. Feast's generation-checked overwrite of `registry.db` needs delete; versioning keeps old generations. |
  | `objectCreator` | `streaming-features/campaigns/verify-fs12-*`, `streaming-history/verify-fs12-*`, `features/_iam-probe/` |

  All writable prefixes were empty before the change.
- **Workload Identity.** KSA `ml-platform/training` → GSA `ewallet-training`
  (`ksa-annotations.json`). The probe Job (`job.yaml`) ran as that identity.
  The probe itself is now the reusable module
  [`iam_probe.py`](../../../../../ai_platform/features/feast/validation/iam_probe.py),
  run with [`feature_store_iam_probe.sh`](../../../../../scripts/mlops/feature_store_iam_probe.sh).
  The exact script that ran is kept in Git history
  (`git show 6e3a81b1:docs/evidence/edai2/feature-store/fs-cloud-20261112/iam/probe.py`).
  `probe-result.json` shows PASS on 7/7 checks:
  - the snapshot manifest read with the pinned SHA, and its prefix listed;
  - a create in the probe prefix succeeded, and a second create was refused (412);
  - writes to `lakehouse/` and `features/offline/` and a list of `lakehouse/` were refused (403).

  The source manifest generation did not change
  (`source-manifest-generation.txt`).

## GKE (`gke/`)

- **Start state.** `cpu-pool` had 0 nodes. 82 workloads were present, 73 of
  them with `replicas=1` and Pending (`workload-replicas-before.txt`).
- **Wake.** The pool was resized to 5 nodes at 08:18:49Z with plain `gcloud`.
  No workload was scaled. The owner chose 5 nodes because the shared pods
  request about 9.1 CPU.
- **Sleep.** `plan-sleep` returned `SAFE_TO_SLEEP` and `cloud_lab sleep`
  resized the pool to 0 (09:11–09:14Z, `nodes-final.txt`). Replicas were
  unchanged except the GKE-managed `konnectivity-agent` and `calico-typha`
  autoscalers.

## Row 11 (`row11/`)

- **Redis scan** (`redis-db-scan.txt`): DB 0 had 173,382 keys, DB 1 had 4, and
  DB 2–15 were empty. Used memory was 52.8 MB of 768 MB.
- **Bootstrap** (`bootstrap/`):
  - `CLAIMED` DB 2 for `fs11-r5gb-20261112`;
  - applied 4 views; registry generation 0 → `1794471871937353`;
  - base load `[12:00, 18:00)` → `1794472043803171`;
  - all four `materialized_end` at 18:00;
  - readback PASS with 155 comparisons and 0 mismatches.
- **Dry run.** `manual__fs11-dry-20261112T084031Z` succeeded and produced the
  token `approve-ff31f721d1f8`.
- **Execute run.** `manual__fs11-20261112T084109Z` finished `success` with 8/8
  tasks:
  - `03-window.json`: `NEW_WINDOW` from 18:00 to 23:00, preflight on DB 2 `OWNED`.
  - `04-materialize.json`: Job `feast-inc-ff31f721d1f8-a1`, generation `1794472043803171 → 1794473045394725`.
  - `05-completion.json`: all four views at 23:00.
  - `06-readback.json`: 155 comparisons, 0 mismatches.
  - `07-provenance.json`: `NEW_WINDOW`, digest image, `e913224f`.
  - `08-datahub-lineage.json`: `PASS`, 7 aspects `MATCH`.
  - `datahub-readback-graphql.json`: an independent GMS query showing 4 snapshot inputs and 4 Online outputs.
- **After the run.** DB 2 holds 204,934 keys. DB 0 still has 173,382. Used
  memory is 114 MB (`redis-after.txt`).

## Row 12 (`row12/`)

- **Campaign.** `verify-fs12-20261112084123`.
  - `render-*.yaml`: 12 objects with digest images.
  - `01-preflight.txt`: `PASS`, claimed DB 8.
  - `02-registry-apply.json`: four views.
- **Flink.** Job `156d3b7e290903464de231b8d5a6f202` was RUNNING with 4 tasks
  (`flink-jobs.json`). Pods ran the pinned digests (`pod-imageids.txt`).
- **Writers.** The publish, history, offline and feast Jobs completed.
  - GCS holds 50 Parquet objects across four views, Kafka offset manifests in
    `_provenance/` and 7 history files (`gcs-*.txt`).
  - Redis DB 8 holds 27 keys: 26 entities and the owner marker.
- **Validator** (`consistency-report.json`):
  - `status` PASS, `entities_compared` 26, `features_compared` 203;
  - direct comparison PASS with 203 comparisons and 0 mismatches;
  - `offline_verification` PASS, `PIT_VALUES_VERIFIED` 26/26;
  - `report_digest` `sha256:7db0e396…`, and an independent `check-report` PASS.
- **Not done on GKE: replay.** `block_a_cloud.sh` has no replay command. Replay
  and idempotency were proven on the local stack (`../fs-20261112/`).
- **Cleanup.** `cleanup --execute` removed only this campaign's labelled
  objects (`cleanup.txt`). GCS data, Redis keys, the registry and evidence were
  kept.
