# Streaming Feature Store verification on GKE — 2026-11-14

Campaign `verify-fs12-20261114063323` on dataset `rehearsal-5gb-v1`, bucket
`ewallet-data-ai-platform-hg26-rehearsal-5gb-data`, cluster `ewallet-dev`. The
owner ran it with the procedure in
[FEATURE_STORE_RUNBOOK.md](../../../../runbooks/edai2/FEATURE_STORE_RUNBOOK.md).
The code was commit `6e3a81b1e847439226fba9d5778988ed6be5995b`, and all images
were pinned by digest:

- `flink-block-a@sha256:bcfb201cc855bf79d2d1279e60a7640b51a37d13a5a0d5b2b73385cbf8e86444`
- `streaming-sinks@sha256:3d227ba938fc4236d1ce1538db0b9fd5d2359b84ab44ecd274d0cd906aa52603`
- Redpanda `@sha256:3387a7ac…b88d`

The files are byte-identical copies of the owner's
`~/ewallet-evidence/fs12-live-20261114T063323Z/` (`SHA256SUMS`). Four log files
there were empty and are not copied: `block-a-topics.log` and the three
`*.error.log` files. The screenshots are in [`../screenshots/`](../screenshots/README.md).

| File | What it records |
|---|---|
| `row12-render.yaml` | The rendered stack and Jobs for this namespace, every image pinned by digest |
| `row12/preflight/*.preflight` | `status=PASS`. Redis DB 9 claimed for this campaign, with the target fingerprint and the owner hash |
| `row12-registry-apply.json` | A new campaign registry, generation `0 → 1794638226937044`, with four FeatureViews |
| `flink-jobs.json` | Job `3dca1e262210328f14d0585f3f8ea70e` `RUNNING`, 4/4 tasks |
| `block-a-publish.log` | The scenario's six phases: 13 normal events, then 1 out-of-order, 1 late-accepted, 1 too-late, 2 replayed or invalid, and 1 closing event |
| `block-a-history.log` | Accepted transactions written to GCS history: 16 records in 7 objects |
| `block-a-offline.log` | Offline writer: 47 rows, 94 objects created (47 Parquet files plus 47 offset manifests), 0 reused |
| `block-a-feast.log` | Feast Online writer: 47 updates written, 0 dead-lettered |
| `validator-job.json` | Validator Job `Complete`, 07:15:59Z → 07:16:46Z |
| `consistency-report.json` / `.log` | `PASS`. 26 entities and 203 features compared, 203 `MATCH`, 0 mismatches, 0 missing, 0 stale. Direct Offline↔Online comparison PASS. Offline `PIT_VALUES_VERIFIED` 26/26. Source-event recomputation PASS. Digest `sha256:1757faa4…a2c6f` |
| `consistency-verdict.json` | The runner's verdict: `PASS` |
| `check-report.txt` | `PASS`. The report's digest was recomputed independently during integration with `data_platform.streaming.verification.cli check-report` |

The 47 Offline rows are the feature updates that Flink emitted for this
campaign's scenario. The writers keep every update, while the validator compares
the latest value of each of the 26 entities. The scenario is a small, generated
workload with campaign-specific keys. It exercises late, out-of-order, replayed
and invalid events; it is not a replay of the 5 GB dataset.
