# Feature Store rows 11 and 12 — evidence of 2026-11-12

Dataset `rehearsal-5gb-v1`, campaign `ab-20261110225456`, profile `optimized`,
bucket `ewallet-data-ai-platform-hg26-rehearsal-5gb-data`. These are raw,
lightweight copies. Large files (Parquet, history) and every `*.log` (ignored
by git) stay in the owner's `~/ewallet-evidence/fs-20261112/`. No screenshot here was generated. Rows 11
and 12 stay open until the owner run in
[OWNER_START_HERE](../../../../edai2/mlops/references/mlops_serving_runbook.md#owner_start_here--rows-11-and-12-rerun-rehearsal-5gb-v1).

## Phase A — immutable Feast snapshot (`phase-a-export/`)

| Item | Value |
|---|---|
| Source feature run | `ab-20261110225456-optimized-feat-r261110225459` (Silver `…-silver-r261110225457`), Delta version 0 for all four tables |
| Selection | `feature_selection.json`, basis `rehearsal-dp3-pass`, digest of the DP3 handoff [`dp3_completion.json`](../../../edai1/spark/rehearsal-5gb-ab-20261110225456/manifests/optimized/dp3_completion.json) |
| Snapshot | `feast-r5gb-20261111-214544` under `features/offline/snapshots/rehearsal-5gb-v1/<feature run>/` |
| Rows | user 18,599,553; account 18,599,553; device 18,599,560; merchant 7,439,478; total 63,238,144 |
| Features | 31; `event_timestamp` UTC microseconds; `created` audit column excluded |
| Event time | 2026-01-01T00:00:01.468763Z to 2026-05-31T23:59:58.182351Z |
| Manifest | `_control/export_manifest.json`, SHA-256 `3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca` |
| Export code | `EXPORT_GIT_SHA` (`4bafd65e`) |
| Independent check | `independent_verification.txt`: PASS |
| Historical retrieval | `historical_retrieval.json`: PASS. Real `get_historical_features()` at 2026-05-20T12:00:00.5Z returned 20 rows, 155 comparisons, 0 mismatches, 0 ambiguous |

Attempts 1 and 2 failed: a missing environment variable, then a GCS response
body error that left the partial prefix `feast-r5gb-20261111-213737` (two
tables, no `_control`). That prefix was not reused, overwritten or deleted.
Their logs are kept in `~/ewallet-evidence/fs-20261112/export/`.

## Row 11 (`row11-dry-run/`, `row11-window-plan.json`)

- `manual__fs11-dry-20261112T065621Z` is the dry-run DagRun on an isolated
  Airflow 3.3.1 (`scripts/mlops/fs11_isolated_airflow.sh`, own home, SQLite
  and port 8094). Result: `success`, 8/8 tasks, approval token
  `approve-ff31f721d1f8`. It checks the manifest digest. It renders the Job
  manifests with the digest image and DataHub `DRY_RUN` records, and publishes
  nothing. `SHA256SUMS` covers its files.
- `manual__fs11-dry-20261112T064633Z` failed at task 8 because the isolated
  runtime lacked `EWALLET_DATAHUB_UV`. The launcher now sets it. That run's
  artifacts are kept here. Its task log is under the isolated `AIRFLOW_HOME`.
- Window plan: base `[2026-05-31T12:00Z, 18:00Z)` with 66,802 entities, then
  incremental `[18:00Z, 23:00Z)` with 141,896 entities. Both were counted from
  the snapshot.
- Not run: claim, apply, base load, the execute DagRun, readback and DataHub
  publication. Two problems blocked them: `kubectl` timed out because this
  machine's IP is not on the control-plane allowlist, and GSA
  `ewallet-training` has no role on the rehearsal bucket.

## Row 12 (`row12-local-verify-fs12-local-20261112062709/`)

This ran locally on real Redpanda, a Flink job with real watermarks, the
history, Offline Parquet and Feast Online writers, and Redis 7.4.2 on loopback
DB 10. The code was commit `2f3a90ff` (`GIT_SHA`).

- `consistency-report.json`: PASS. 24 entities and 189 direct Offline↔Online
  comparisons, 0 mismatches. Offline PIT was `PIT_VALUES_VERIFIED` for 24/24.
  `check-report` PASS (digest `sha256:b9e61998…`).
- Replay (`consistency-report-after-replay.json`): all
  three sink groups were rewound and re-read. PASS with the same 189
  comparisons and 0 mismatches. The Offline Parquet file hashes did not change
  (`pre-`/`post-replay-files.sha256`) and DB 10 kept 24 keys. The history sink
  wrote 3 extra batch files because its batch boundaries changed, and the
  validator dropped 16 repeats.
- Local, not GKE evidence. FS12 screenshots must come from the GKE run.

## Images (`IMAGES.txt`)

Digests built from `2f3a90ff` and pushed to `ewallet-platform`. The dry run
used `feast-online@sha256:af496703…`. The owner run resolves the digests
for its own `APPROVED_SHA` (OWNER_START_HERE, Step 1).
