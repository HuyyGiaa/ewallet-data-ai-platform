# Canonical streaming Block A

## Scope and immutable contracts

Block A is `deterministic generator -> Redpanda -> Flink -> immutable history +
Feast Online/Redis`. It owns the 31 historical/behavioral features only. DP1,
DP2, DP3, request-time fields, the six derived features, the 41-column model
contract, model inference, KServe, KFP and MLflow remain unchanged.

The source of truth for names and types is
`ai_platform/features/feast/schema.py`; the source of truth for historical
semantics is `data_platform/processing/spark/gold/features/`. Every rolling
window is left-inclusive and right-exclusive: `[T-window,T)`. Events at T,
including peers with the same timestamp, cannot enter their own snapshot.

## Legacy recovery and migration classification

| Legacy component | Decision | Migration status | Reason |
|---|---|---|---|
| `generation/src/streaming/event_factory.py` | ADAPT | TEMPORARY_REFERENCE | Useful business fields, but global RNG/UUID4 and local mutable state are not deterministic contracts. |
| `generation/src/streaming/state_loader.py` | REPLACE | SAFE_TO_REMOVE_AFTER_MIGRATION | Hard-wired local Parquet and balance snapshots. |
| `generation/src/streaming/streaming_generator.py` | REPLACE | REPLACED_BY_CLOUD | No message key/schema version/trace ID and mixes scenarios, transport and pacing. |
| `generation/src/streaming/constants.py` | REPLACE | SAFE_TO_REMOVE_AFTER_MIGRATION | Hard-coded localhost/topic. |
| `processing/flink/streaming_pipeline.py` | REPLACE | TEMPORARY_REFERENCE | Demo has 5m tumbling aggregate/print sinks, not the 31 rolling features. |
| `transaction_stream_baseline.py`, `transaction_stream_parsed.py` | REMOVE later | SAFE_TO_REMOVE_AFTER_MIGRATION | Console-only tutorials. |
| `processing/flink/burst_monitor.py` | KEEP concept | TEMPORARY_REFERENCE | Burst/backpressure evidence remains useful; runtime wiring is legacy. |
| Kafka connector JAR for Flink 1.19 | ADAPT | TEMPORARY_REFERENCE | Version must be pinned with the eventual Flink image. |
| local Docker Compose messaging files | KEEP for dev only | TEMPORARY_REFERENCE | Useful local fixture, never cloud source of truth. |
| F5 Spark feature builders | KEEP | STILL_CANONICAL | Verified semantic oracle for all 31 features. |
| F6 Feast Offline schema/retrieval | KEEP | STILL_CANONICAL | Training/PIT source of truth. |
| Feast Online/Redis writer, history sink, retry/DLQ | MISSING legacy | new canonical modules | Implemented as explicit Block A boundaries. |

No legacy files are deleted in this milestone.

## Canonical architecture and dependency direction

```text
contracts/config <- deterministic generator <- Redpanda publisher
        |
        v
Redpanda raw -> Flink parse/dedup/event-time buffer/entity rekey
                    |              |                    |
                    v              v                    v
             accepted topic   late topic          online-update topic
                    |              |                    |
                    v              v                    v
          immutable history  reconciliation       Feast PushSource
                                                      |
                                                      v
                                               Redis Online Store
```

Flink internally fans a validated event into user, account, device and, for an
eligible payment, merchant keyed state. There is one broker topology. Redis
stores only Feast-serialized latest/current feature rows; it is never raw
history, a computation engine, training storage, or a model registry.

## Event contract

The versioned `TransactionEvent` requires `event_id`, `transaction_id`,
`user_id`, `account_id`, `device_id`, nullable `merchant_id`, nullable
`counterparty_account_id`, type, amount, currency, status, channel, old/new
balance, `event_time`, `arrival_time`, `schema_version` and `trace_id`.

- `event_time`: UTC business time used for all feature windows.
- `arrival_time`: UTC generator/broker arrival used for operational latency,
  retry and dedup TTL; never used as feature time.
- `transaction_id`: business dedup identity and raw-topic key.
- `event_id`: immutable event-envelope identity; a duplicate retains both IDs.
- payment requires a merchant. Timestamps without timezone are rejected.

`DeterministicScenarioGenerator(seed=...)` deterministically emits normal,
duplicate, out-of-order, late and invalid fixtures. It uses local RNG/UUID5,
so it does not mutate module-global random state.

## Redpanda topology

| Role | Topic | Partitions | Retention | Key |
|---|---|---:|---:|---|
| input | `ewallet.transactions.raw.v1` | 12 | 7d | `transaction_id` |
| retry | `ewallet.transactions.retry.v1` | 12 | 3d | original `transaction_id` |
| DLQ | `ewallet.transactions.dlq.v1` | 6 | 30d | original `transaction_id` |
| valid late reconciliation | `ewallet.transactions.late.v1` | 12 | 30d | `transaction_id` |
| accepted history | `ewallet.transactions.accepted.v1` | 12 | 30d | `transaction_id` |
| online updates | `ewallet.features.online.v1` | 12 | 3d | `entity_type:entity_id` |

The raw key places duplicates and retries together. It cannot simultaneously
order four entity domains, so Flink performs the minimum internal rekey/fanout
to `user:*`, `account:*`, `device:*` and eligible `merchant:*`. Consumer groups
are versioned in `configs/streaming/block-a.yaml`.

## Event time, lateness and state

The default watermark is `max(event_time)-2m`, allowed lateness is 10m, and an
idle source is marked after 30s. A timestamp group is emitted only after the
watermark finalizes it. This buffering makes all same-timestamp peers observe
one shared pre-event snapshot.

- ON_TIME: at or ahead of the watermark; history and online state.
- LATE_ACCEPTED: behind watermark but within 10m; history and corrected state
  before affected timestamp groups are finalized.
- TOO_LATE: beyond allowed lateness; retained in history and copied to the
  dedicated late/reconciliation topic, never sent to online state or DLQ.

Dedup keys `transaction_id` with 48h TTL, covering the configured 24h legacy
late range, retry time and margin. After expiry a replay can be accepted again;
the historical batch path must retain Silver's canonical transaction-ID dedup.
Feature state retains 30d+10m and cleans older events as event time advances.
Counters expose inputs, duplicates, removed duplicates and unique outputs.

## All 31 feature ownership decisions

All 31 are **HYBRID**. Flink can maintain them exactly only after history or
dimension bootstrap. None is honestly REALTIME from empty state; none must
remain permanently MATERIALIZED_BATCH once correct bounded state exists.

| Feature | Entity | Definition/window | DP3 builder | Required state | Bootstrap | Owner |
|---|---|---|---|---|---|---|
| user_tx_count_5m | user | count `[T-5m,T)` | user_features | 5m events | 30d replay | HYBRID |
| user_tx_count_1h | user | count `[T-1h,T)` | user_features | 1h events | 30d replay | HYBRID |
| user_tx_count_24h | user | count `[T-24h,T)` | user_features | 24h events | 30d replay | HYBRID |
| user_amount_sum_1h | user | amount sum `[T-1h,T)` | user_features | 1h amounts | 30d replay | HYBRID |
| user_amount_observation_count_30d | user | non-null amount count `[T-30d,T)` | user_features | 30d amounts | 30d replay | HYBRID |
| user_avg_amount_30d | user | mean `[T-30d,T)` | user_features | 30d amounts | 30d replay | HYBRID |
| user_std_amount_30d | user | sample stddev `[T-30d,T)` | user_features | 30d amounts | 30d replay | HYBRID |
| user_failed_rate_24h | user | failed/count `[T-24h,T)` | user_features | 24h status | 30d replay | HYBRID |
| user_distinct_merchants_24h | user | distinct non-null merchants `[T-24h,T)` | user_features | merchant multiset | 30d replay | HYBRID |
| account_tx_count_5m | account | count `[T-5m,T)` | account_features | 5m events | 30d replay | HYBRID |
| account_tx_count_1h | account | count `[T-1h,T)` | account_features | 1h events | 30d replay | HYBRID |
| account_tx_count_24h | account | count `[T-24h,T)` | account_features | 24h events | 30d replay | HYBRID |
| account_amount_sum_1h | account | amount sum `[T-1h,T)` | account_features | 1h amounts | 30d replay | HYBRID |
| account_amount_sum_24h | account | amount sum `[T-24h,T)` | account_features | 24h amounts | 30d replay | HYBRID |
| account_amount_observation_count_30d | account | amount count `[T-30d,T)` | account_features | 30d amounts | 30d replay | HYBRID |
| account_avg_amount_30d | account | mean `[T-30d,T)` | account_features | 30d amounts | 30d replay | HYBRID |
| account_std_amount_30d | account | sample stddev `[T-30d,T)` | account_features | 30d amounts | 30d replay | HYBRID |
| account_failed_rate_24h | account | failed/count `[T-24h,T)` | account_features | 24h status | 30d replay | HYBRID |
| account_seconds_since_last_tx | account | T minus latest event strictly before T | account_features | latest prior time | 30d replay + latest pre-window account anchor | HYBRID |
| device_age_seconds | device | `max(0,T-first_seen_at)` | device_features | first-seen dimension | device dimension | HYBRID |
| device_tx_count_1h | device | count `[T-1h,T)` | device_features | 1h events | 30d replay | HYBRID |
| device_tx_count_24h | device | count `[T-24h,T)` | device_features | 24h events | 30d replay | HYBRID |
| device_amount_sum_24h | device | amount sum `[T-24h,T)` | device_features | 24h amounts | 30d replay | HYBRID |
| device_failed_rate_24h | device | failed/count `[T-24h,T)` | device_features | 24h status | 30d replay | HYBRID |
| merchant_tx_count_10m | merchant | payment count `[T-10m,T)` | merchant_features | 10m payments | 30d replay | HYBRID |
| merchant_tx_count_1h | merchant | payment count `[T-1h,T)` | merchant_features | 1h payments | 30d replay | HYBRID |
| merchant_tx_count_24h | merchant | payment count `[T-24h,T)` | merchant_features | 24h payments | 30d replay | HYBRID |
| merchant_unique_users_10m | merchant | distinct users `[T-10m,T)` | merchant_features | user multiset | 30d replay | HYBRID |
| merchant_unique_users_1h | merchant | distinct users `[T-1h,T)` | merchant_features | user multiset | 30d replay | HYBRID |
| merchant_amount_sum_1h | merchant | payment sum `[T-1h,T)` | merchant_features | 1h amounts | 30d replay | HYBRID |
| merchant_avg_amount_24h | merchant | payment mean `[T-24h,T)` | merchant_features | 24h amounts | 30d replay | HYBRID |

REALTIME: none. MATERIALIZED_BATCH: none after bootstrap. HYBRID: all 31.

## Bootstrap and offline-to-online materialization

Canonical Silver is the narrowest correct source: `silver.transactions` is the
deduplicated event-grain table directly consumed by the verified F5 builders
and contains every entity, amount/status/type/channel/balance field plus
`timestamp` and `ingested_at`; `silver.devices` supplies `first_seen_at`.
Gold is downstream, wider, and unnecessary for reconstructing these states.

At an explicit cutover T0:

1. DP3's immutable run and Feast Offline remain the semantic source.
2. Feast materializes only the latest complete row per entity/view at or before
   T0 into Redis. It does not copy history.
3. With online writes disabled, the first deployment reads the immutable,
   run-scoped canonical Silver `transactions` table over `[T0-30d,T0)`, the
   latest pre-window Silver transaction timestamp per account, and Silver
   `devices.first_seen_at`. It adapts those rows deterministically and replays
   timestamp groups ordered by `(event_time, transaction_id)` into Flink state.
4. The parity gate compares selected entities/timestamps against DP3, including
   nulls, boundaries and same-time peers.
5. After a clean checkpoint, ownership switches atomically to streaming events
   at/after T0 and full FeatureView rows are pushed.

The Redis materialization gives immediate reads but cannot reconstruct rolling
state. Silver replay/dimension load solves that separately. The accepted topic
is not assumed to contain pre-deployment history. During normal operation the
raw Redpanda topic feeds Flink. A restart restores the last checkpoint/savepoint
and resumes the retained raw stream; accepted history can assist reconciliation
but is not the first-deployment bootstrap source. A warm-up-only alternative is
not canonical because it would serve incomplete 30-day features.

## Feast Online and Redis

The dedicated online Feast repository mirrors the exact four entities, four
views, names and types. Each view uses a Feast 0.66 `PushSource` whose batch
source is the DP3-derived Parquet snapshot, and is `online=True`. Redis is
configured by `EWALLET_REDIS_CONNECTION_STRING`; no application module knows a
raw Redis key layout. Four independent time controls are explicit:

- FeatureView TTL is 30d+10m and bounds Feast feature-history/materialization
  semantics.
- Serving freshness is checked from the feature timestamp with a 10m stale
  threshold; Redis arrival time is not feature time.
- `key_ttl_seconds: null` means Redis keys do not physically expire in this
  pilot contract. Feast 0.66 calls Redis `EXPIRE` only when that option is a
  positive integer.
- Flink rolling state TTL is separately 30d+10m.

Persistent Redis keys may therefore return an old value unless the retrieval
boundary applies the timestamp freshness policy. This is documented behavior,
not a custom key-management layer.

Flink emits complete FeatureView update envelopes to the online topic. The
decoupled writer invokes `FeatureStore.push(<view>_push, frame,
to=PushMode.ONLINE)`. Complete rows avoid mixing timestamps within a view;
Feast entity serialization and update timestamp rules protect retrieval from
older arrivals. Cold or expired entities are MISSING, nullable statistics stay
null, and zero-valued counts/sums remain zero.

## Historical sink

Every unique valid event, including TOO_LATE, enters the accepted topic with
event/transaction identity, both timestamps, entities, schema version, trace
ID, disposition and original payload. A future connector writes an immutable,
run-scoped streaming-history prefix configured by environment. It must never
overwrite canonical DP1/DP2/DP3 prefixes. TOO_LATE also enters the dedicated
late topic with the original payload, event/arrival times, observed watermark,
schema version, reason and trace ID. Batch reconciliation keeps the same
transaction-ID dedup rule.

## Retry and DLQ

Transient broker/sink/timeouts are retryable up to three attempts. Only invalid
schema/business records, non-retryable processing failures, exhausted retries
and poison messages go to DLQ. Valid TOO_LATE events go to history plus the
late/reconciliation topic. Failure envelopes preserve the original payload,
original event time, attempt count, reason and trace ID. Poison messages
therefore cannot block the main consumer group.

## Offline/online parity

The deterministic oracle classifies each feature comparison as MATCH,
MISMATCH, STALE, MISSING or NOT_REALTIME_OWNED. Fixtures compare value,
feature timestamp and cold/null state. Required gates cover exact lower/upper
window boundaries, same-timestamp peers, late accepted corrections, all four
entity mappings and materialized-then-streaming cutover. A column-name-only
comparison is insufficient.

## Controlled Flink and retrieval evidence

Baseline and optimized profiles share watermark, allowed lateness, dedup TTL,
history TTL, topics and feature logic.

| Setting | Baseline | Optimized | Justification |
|---|---:|---:|---|
| parallelism | 1 | 4 | controlled partition/state concurrency |
| checkpoint interval | 120s | 30s | bounded recovery/freshness trade-off |
| incremental checkpoints | false | true | reduce repeated state transfer |
| sink batch size | 1 | 250 | amortize online/history writes |

Later evidence records input/output events/s, consumer lag, end-to-end p50/p95/
p99, feature freshness, backpressure, on-time/late/too-late, duplicate counts,
retry/DLQ, checkpoint duration/failure/state bytes and parity outcomes. The
retrieval comparison records direct historical versus Feast Online/Redis p50,
p95, p99, throughput, error, stale, missing and parity rates. These are package
comparisons unless an ablation isolates one setting.

## Future deployment and rollback sequence

These commands are a future USER_RUN boundary; none were executed here.

```bash
export EWALLET_REDPANDA_BOOTSTRAP_SERVERS='<private-bootstrap>'
export EWALLET_FEAST_OFFLINE_DATA_ROOT='gs://<immutable-dp3-run>/feast'
export EWALLET_FEAST_REGISTRY='gs://<registry-bucket>/block-a/registry.pb'
export EWALLET_REDIS_CONNECTION_STRING='<private-redis-host>:6379'
export EWALLET_FEAST_ONLINE_REPO="$PWD/ai_platform/features/feast/online_repo"
export EWALLET_BOOTSTRAP_SILVER_ROOT='gs://<data-bucket>/lakehouse/runs/<silver-run>/silver'
export EWALLET_BOOTSTRAP_SILVER_RUN_ID='<silver-run>'
export EWALLET_DATASET_VERSION='<dataset-version>'

# Create only the six versioned topics from configs/streaming/block-a.yaml.
# Apply the isolated online repo, then materialize to the explicit T0.
feast -c "$EWALLET_FEAST_ONLINE_REPO" apply
feast -c "$EWALLET_FEAST_ONLINE_REPO" materialize '<T0-minus-30d>' '<T0>'

# Submit the pinned Flink artifact with profile=baseline first. Replay immutable
# Silver transactions over [T0-30d,T0) with online emission disabled, load the
# account anchors and Silver device first_seen_at, run the parity gate,
# checkpoint, then restart/cut over with profile=optimized.
```

Deployment must pin the Flink image, Kafka connector and artifact digest before
submission. Rollback stops online emission, restores the last clean checkpoint,
keeps accepted history/late/retry/DLQ intact, and rematerializes the prior immutable
DP3 snapshot. Topic deletion and Redis flush are never automatic rollback
steps.

## Known limitations and gates

- The pure-Python state implementation remains the deterministic semantic
  oracle. The production PyFlink managed-state topology is now wired in source,
  but still requires runtime/integration verification before rollout.
- Live Redpanda, checkpoint recovery, Feast/Redis and lakehouse connector tests
  are intentionally deferred because deployment is prohibited in this task.
- Full 30-day replay cost/state size is not yet measured. It is a mandatory
  capacity gate, not permission to replace correct bootstrap with latest Redis
  aggregates.
- The old local modules remain until both offline and online paths are deployed,
  reconciled and parity-verified in a separate cleanup milestone.

## Runtime source map and delivery guarantees

`data_platform.streaming.flink.job` constructs the executable DataStream graph.
`ParseAndValidate`, `ManagedDeduplicate`, `EventTimeDisposition`,
`EntityFanOut`, and `ManagedHistoricalFeatures` connect raw/retry inputs to the
four entity states and all 31 outputs. Kafka Table sinks carry `message_key`
separately from `payload` for accepted, late, online, retry, and DLQ topics.
Stable operator UIDs are the state-restoration contract.

Flink checkpoints cover managed state and source offsets. Kafka output,
immutable GCS history, and Feast pushes have independent commit boundaries, so
Block A does not claim end-to-end exactly once. Stable business keys,
generation-preconditioned history objects, Feast feature timestamps, and
post-write offset commits make replay safe to reconcile.

First deployment is driven by `bootstrap.service`: it validates PASS/COMPLETE GCS
provenance manifest; scans only `[T0-30d,T0)` plus device first-seen and account
anchors; externally sorts by `(event_time, transaction_id)` with bounded memory;
and emits bootstrap envelopes with history/online writes disabled. The
replay uses partition 0 while retaining each transaction ID as its Kafka key,
so its completion marker cannot overtake replay records on another partition.
`BOOTSTRAP_COMPLETE`, zero bootstrap consumer lag, and the next completed
externalized checkpoint together form the handoff gate. The operator then
creates a retained savepoint and starts `NORMAL_STREAMING` from it. Missing immutable
identity, manifest, cutover, checkpoint storage, or recovery savepoint fails
closed.

Concrete source entrypoints:

- `python -m data_platform.streaming.generator.cli`
- `python -m data_platform.streaming.bootstrap.service`
- `python -m data_platform.streaming.flink.job`
- `python -m data_platform.streaming.sinks.historical_service`
- `python -m data_platform.streaming.feast.service`

They target the tracked Flink 1.19 Kafka connector and Feast 0.66 contract.
Construction, live connector compatibility, checkpoint recovery, and delivery
remain explicit USER_RUN verification gates; no code or service was executed in
this source-only milestone.

Future deployment order is: start the FIRST_DEPLOY_BOOTSTRAP Flink job, run the
Silver replay feeder, wait for `BOOTSTRAP_COMPLETE`, zero replay lag and a
completed externalized checkpoint, create a savepoint, stop the bootstrap job,
then restore the identical stable-UID topology in NORMAL_STREAMING mode. A
restart uses RESTART_RECOVERY plus `--savepoint-path`; it never starts with
empty state. The independent history and Feast consumers start only after the
cutover. Their required arguments are discoverable with the standard `--help`
of each module and all credentials remain runtime environment concerns.
