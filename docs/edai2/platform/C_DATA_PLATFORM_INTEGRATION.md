# Data Platform Integration Contract (C1, C2, C3)

This document records what the audit found, the ownership rules that let batch
materialization and streaming share one online store, and the state of each workstream.
Commands are in the [runbook](../../runbooks/edai2/C_DATA_PLATFORM_RUNBOOK.md).

**Status of this document:** everything below was validated locally or read from the cloud
without changing it. Nothing here is cloud runtime evidence, and no rubric row is promoted.

## 1. Execution boundaries

| Component | Where it runs today | Source of that statement |
|---|---|---|
| Airflow 3.3.1 | Local control plane on the owner's machine, stopped during this work | `scripts/start_airflow_local.sh`; no Airflow workload in any manifest |
| Feast Online operator | Kubernetes Job in `ml-platform` | `scripts/mlops/feast_online_job.sh` |
| Redis | StatefulSet in `feature-store`, not Memorystore | `infra/kubernetes/feast-online/redis.yaml`; the Memorystore API is disabled |
| Redpanda, Flink | **Local Docker Compose only**; no cloud manifest existed before this work | `infra/docker/flink-benchmark/compose.yml` |
| Dataproc | Serverless batches; no cluster, no persistent History Server | `gcloud dataproc`; `sparkHistoryServerConfig` is empty |

This audit used the IP-allowlisted control-plane address, which did not include this machine, so
it inspected nothing inside the cluster. The cluster was in fact reachable through its
IAM-authenticated DNS endpoint. Everything in the cluster column therefore comes from source, not
observation, and should be read back before it is relied on. GCS and Dataproc metadata were read
directly.

## 2. Who writes what

There is one set of four FeatureViews and one Redis key per entity and view. C1 and C2 share
them, so they are ordered by event time rather than separated by key.

| Question | Answer |
|---|---|
| Who owns a feature key? | All 31 features are `HYBRID`. Batch owns event time before the cutover, and Flink owns it from the cutover on |
| Who writes historical feature rows? | DP3 writes immutable batch snapshots. The streaming offline writer consumes Flink's complete FeatureView-update topic and writes create-only, campaign/run-scoped Parquet rows; Flink itself still does not write GCS |
| Who writes real-time online values? | The Feast writer, through `FeatureStore.push(..., PushMode.ONLINE)` |
| Who else may write Redis? | Only `FeatureStore.materialize`. No component writes raw Redis keys |
| Are FeatureViews shared? | Yes. `online_repo/feature_definitions.py` binds each view to a `PushSource` whose batch source is the snapshot |
| What is the conflict rule? | Newest event timestamp wins, per entity and view, at nanosecond precision. An equal timestamp keeps the stored row |
| What stops old history replacing new state? | Feast's Redis store skips a row that is not newer. Proven against real Redis in `tests/integration/test_feast_online_local.py` |
| Is the offline store immutable? | Batch snapshots are immutable by run. Streaming feature rows use deterministic logical-row objects and create-only offset manifests; identical replay is reused and conflicting replay fails |
| How are late events handled? | Within allowed lateness they enter state. Beyond it they stay in history, go to the late topic, and never change online state |

### Why the guard is not enough alone

Feast reads the stored timestamp, then writes. That sequence is not atomic, and the Feast source
says so. If a materialization and a push touch one entity in the same instant, an older row can
land last. The next streaming event for that entity repairs it, and
the online value would otherwise stay stale until then.

**Rule:** pause the Feast writer while a materialization runs. Updates wait in the online topic,
which keeps them for 72 hours, and replaying them is safe. The window also cannot pass the
cutover, because `MaterializationRequest` refuses `target_end > streaming_cutover`.

## 3. Delivery guarantees

These are the guarantees the code provides. End-to-end exactly-once is not claimed.

| Hop | Guarantee | Consequence |
|---|---|---|
| Redpanda to Flink state | Exactly-once through checkpoints | Restored in the savepoint run |
| Flink to Kafka sinks | At-least-once (`sink.delivery-guarantee`) | Downstream must tolerate repeats |
| History writer | At-least-once; commit after the object exists | Readers deduplicate by `transaction_id` |
| Offline feature writer | At-least-once consumer with create-only logical-row and offset objects; offset commits after both exist | Identical replay is idempotent; a conflicting logical row is not acknowledged |
| Feast writer | At-least-once; commit after the push | Repeats are ignored by the timestamp guard |
| Materialization | Repeatable; the registry advances only after every view finished | A failed job can rerun the same window |

## 4. C1: incremental materialization

**Reused:** `online_operator.py` (`materialize-incremental`, `parity`), `feast_online_job.sh`,
`registry_mirror.py`, `bounded_dask.py`.

**Added:** `materialization_plan.py` holds every decision, `ops/mlops/feast_incremental.py` runs
one stage per task, and `feast_incremental_materialization.py` is the DAG.

| Retry case | How it is told apart | Behaviour |
|---|---|---|
| Window already finished | Every view reached the target | `COMPLETED`: no Job, readback still runs |
| Some views behind | Some views below the target | `PARTIAL`: the operator resumes each view from its own end |
| New window | All views below the target | `NEW_WINDOW` |
| Snapshot exhausted | Every view is past the snapshot's last event | `NO_NEW_DATA`: recorded, and no load is claimed |
| Job still running after a worker died | Job with this window label is active | Joined, not duplicated |
| Job succeeded, stage artifact missing | Job with this window label succeeded | Reused |
| Job failed | Job with this window label failed | Next attempt |

### Findings

1. **The live registry has no next window.** The snapshot ends at `2026-05-31T23:59:58.617Z` and
   the registry already reaches `2026-06-01T00:00:00Z`. A window that runs past the snapshot would
   be recorded as loaded while loading nothing, and a later snapshot's rows at or before that end
   would be skipped silently. The planner now rejects or classifies this. A real incremental run
   needs a separate registry, described in the runbook.
2. **Published parity timestamps are wrong.** Feast returns `__ts` as whole epoch seconds, and the
   operator read that as nanoseconds. `docs/evidence/cloud/mlops-campaign-20261031/feast_online_parity.json`
   therefore shows `1970-01-01T00:00:01.78…`. The 155 value comparisons are unaffected, because
   they do not use that field. The code is fixed. The published artifact was left unchanged.
3. **Parity could not coexist with streaming.** A row newer than the cutover counted as a mismatch.
   Such entities are now reported as `superseded_by_streaming`, and a run with nothing left to
   compare fails.

## 5. C2: Flink dual-store path

**Already present:** the Flink job, deduplication, event-time classification, 31-feature state,
retry and DLQ routing, the history writer and the Feast writer.

**Added:** `ISOLATED_VERIFICATION` mode, a deterministic scenario, an independent consistency
validator, bounded and signal-safe sinks, a locked sink image, GKE manifests, and a separate
offline feature writer that persists complete FeatureView updates as Feast-compatible Parquet.

### Findings

1. **Event time was not used by production paths.** `from_source` keeps only the Java watermark
   strategy, and PyFlink applies a Python timestamp assigner only in
   `assign_timestamps_and_watermarks`. Watermarks therefore followed the broker's record time. In a
   real run the watermark sat at 2026-11-06 for events dated 2026-06-01, and the newest event was
   classified `TOO_LATE`. Tests missed it because they use a fake `from_source`.
   **Fixed** for every mode except `BENCHMARK_REPLAY`, which has its own logical watermark and
   whose historical evidence depends on it. The cost is that watermarks are now per source subtask,
   not per Kafka partition.
2. **`BENCHMARK_REPLAY` keeps the old source path.** Its classification uses logical watermarks, but
   from reading the code its timers still follow broker time. This was not measured. Its published
   results are unchanged and should be read with that in mind.
3. **Short runs wrote no history.** The writer flushed only on batch size, and SIGTERM skipped its
   `finally` block. It now flushes on a timer and stops cleanly on SIGTERM.
4. **The Feast writer lacked the registry mirror.** `ewallet-training` holds object permissions on
   the bucket, and `registry_mirror.py` exists because Feast's GCS store also needs
   `storage.buckets.get`, so the writer would have failed on GKE. It now mirrors the registry.
5. **One bad update stopped the Feast writer.** Malformed updates now go to the dead-letter topic.
   A Redis failure still stops it without committing, so the update is retried.
6. **Legitimate nulls were reported as missing.** `compare_feature` treated a null value in an
   existing row as `MISSING`, which affects the standard deviation of one observation.

7. **Watermarks do not survive a restart.** A savepoint restores keyed state, but not the watermark.
   After a restore the watermark starts again, so events arriving soon after it are classified more
   leniently: in a real run a late event was recorded `ON_TIME` and a too-late event was recorded
   `LATE_ACCEPTED`. No data was lost and all 196 features still matched history, because such events
   entered state and history alike. This is Flink behaviour and was not changed. Lateness metrics
   right after a restart are not reliable, and the recovery test therefore restarts after the late
   phases.

### What the offline side is

There are now two independent offline records. The accepted-event history remains the source-event
audit path, and replaying it through the reference state must still reproduce Feast Online. The
new offline writer separately consumes complete FeatureView updates and persists one canonical
Parquet schema per view, with provenance in sidecar offset manifests so it does not leak into model
features. The validator both preserves the source-event recomputation check and performs a genuine
Feast `get_historical_features` query over the persisted Parquet, followed by a direct entity,
timestamp and feature-value comparison with Feast Online. File existence or a pandas read is not
accepted as proof. This source implementation has not yet been exercised on GKE/GCS.

## 6. C3: Gold equivalence

### Root cause

The two runs used different physical plans: AQE off with 200 shuffle partitions, against AQE on
with 400. Spark sums doubles in plan-dependent order, and IEEE-754 addition is not associative, so
the last bits of three aggregates differ. The exact fingerprint then fails, as it should.

| Column | Rows | Rows differing | Max relative | Max absolute | Max ULP |
|---|---:|---:|---:|---:|---:|
| `feat_user_90d.f_user_avg_transaction_amount_90d` | 46,200,000 | 8,030,070 | 6.7e-16 | 1.9e-9 | 5 |
| `opt_merchant_performance.total_amount` | 1,108,800 | 742,872 | 2.6e-15 | 5.7e-6 | 20 |
| `opt_merchant_performance.avg_amount` | 1,108,800 | 696,821 | 2.6e-15 | 2.7e-9 | 23 |

These are numerical artifacts, not semantic errors:

- every integer, string and timestamp column matches exactly, as do the keys;
- `f_user_failed_transaction_rate_90d`, also a double, matches exactly, which fits a ratio of two
  exact integers;
- the mean signed error is about 1e-19, so there is no bias;
- no business invariant is violated on either side;
- the largest absolute difference is 0.0000057 VND on a total of about 2.2 billion VND.

### The uncommitted validator is wrong

The working tree holds an uncommitted change to `ops/gcp/summarize_spark_output.py` that hashes
floats formatted with `%.12g`. Its unit tests pass, but it would still fail on this data, because
values a few ULP apart can fall on either side of a rounding boundary. Measured on the real pair:

| Column | Rows that still differ after `%.12g` |
|---|---:|
| `avg_amount` | 8,726 |
| `f_user_avg_transaction_amount_90d` | 146 |
| `total_amount` | 0 |

That change was left in place, because it belongs to unrelated uncommitted work. It should not be
committed for this purpose.

### The replacement

`data_platform/quality/gold_equivalence.py` joins on the business key and compares every column.
A tolerance applies to exactly those three columns. Everything else, including other floats, must
match bit for bit. `TOLERANCE_EQUIVALENT` is a separate verdict: the failed exact result stays
visible, and the verdict adopts nothing.

**Result on the real outputs:** `TOLERANCE_EQUIVALENT`, 0 rows over tolerance, 0 unresolved
differences. The comparison took 91 seconds with a peak of 10 GB. It read local copies whose Delta
commits are byte-identical to GCS. The small table was also compared directly from `gs://`. The
large table has not been, so this is not yet the cloud acceptance run.

## 7. Local validation

These runs used real software on one machine: Redpanda, Flink 1.19, Feast 0.66, Redis and Airflow
3.3.1. They show readiness. None of them is cloud evidence.

| Check | Result |
|---|---|
| Earlier dual-store scenario | PASS: 189 of 189 source-derived features match, 0 timestamp mismatches, every disposition as expected |
| Earlier replay of two sink topics | PASS: 16 repeated history records dropped, 45 repeated Redis writes changed nothing |
| New persisted Offline writer | Unit/integration PASS: deterministic create/reuse/conflict behavior and real Feast historical retrieval across all four views; cloud runtime pending |
| Stop with savepoint, then restore | PASS: 196 of 196 features match. The restored job caught the replayed event and fired 47 updates from restored state |
| Real Feast operator and Redis | 17 tests pass, including an old window against newer streaming state |
| Gold on real outputs | `TOLERANCE_EQUIVALENT`, 0 rows over tolerance |

## 8. Conflicts and limits

| Topic | State |
|---|---|
| Batch and streaming timestamps | Agree: UTC, `[T-window, T)`, feature timestamp equals the event timestamp |
| Entity keys and feature names | One source, `FEATURE_SPECS`; types come from `integer_features` |
| Fraud API reads | `_timestamp` already treats `__ts` as epoch seconds |
| `created` column | The contracts in the working tree declare it. **No cloud output has it.** It belongs to uncommitted work outside C1 to C3 |
| Feast `__ts` | Whole seconds, so the validator compares timestamps at that precision |
| Cross-partition skew | Must fit within the 120-second out-of-orderness, after the event-time fix |
| GKE stack | Single broker, one TaskManager, ephemeral storage. Verification only, not production |
| Dataproc Spark UI | No persistent History Server. Logs are read offline or through a local History Server |
| Gold winner | Not selected. `select_spark_stage_winner` still requires the exact gate |
