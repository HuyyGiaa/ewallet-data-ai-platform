# Flink baseline vs optimized — local benchmark runbook

Status: **SOURCE_READY** (offline tests only). No performance number exists until
the owner runs this on the benchmark PC; nothing here is `PERFORMANCE_VERIFIED`.

## What is compared

| Knob (`configs/streaming/flink-*.yaml`) | baseline | optimized |
|---|---|---|
| parallelism | 1 | 4 |
| checkpoint interval | 120 s | 30 s |
| state backend | rocksdb | rocksdb |
| incremental checkpoints | false | true |

Both YAML files are loaded at runtime by `settings.load_profile`; the job prints
`config_sha256` at submit and `run.json` records it. Event-time, dedup and
history semantics come only from `configs/streaming/block-a.yaml`, so they are
identical for both profiles. Validation rejects unknown keys, `incremental_checkpoints: true`
with `hashmap` (Flink would ignore it silently), and `sink_batch_size`.

**`sink_batch_size` was removed.** The earlier `250` value was never wired to
any connector. The job writes through the append-only `kafka` SQL connector,
which has no row-count buffer (`sink.buffer-flush.max-rows` exists only on
`upsert-kafka`, which has different, keyed-changelog semantics), and Kafka
producer `batch.size` is byte-based. Both profiles use identical sink settings.

## Python dependencies (uv)

- Owner project: `infra/docker/flink-benchmark/pyproject.toml` + `uv.lock`
  (virtual project, `package = false`, same pattern as `infra/docker/llm-agent-tooling`).
- Python `>=3.10,<3.12`; image uses CPython 3.10 (Ubuntu 22.04 Flink base).
- Default deps: `confluent-kafka==2.6.1`, `pyyaml==6.0.2`. Extra `flink`:
  `apache-flink==1.19.1`. Dev group: `pytest==8.3.5`.
- JVM pins (not in uv.lock): `flink:1.19.1-scala_2.12-java11@sha256:ab8afa8f…`,
  committed `flink-sql-connector-kafka-3.3.0-1.19.jar`, Redpanda digest shared
  with `infra/docker/compose.messaging.yml`.
- Image install: `uv sync --locked --no-dev --extra flink --no-install-project`.
  Six locked pure-Python sdists are built from hashed sdists with
  `setuptools==75.8.0`/`wheel==0.45.1` pinned via `build-constraint-dependencies`.

Optional host-side checks (WSL2, only if uv is installed):

```bash
uv --version
uv lock --check --project infra/docker/flink-benchmark
uv sync --locked --project infra/docker/flink-benchmark          # no PyFlink; tests only
PYTHONPATH=. uv run --locked --project infra/docker/flink-benchmark python -m pytest -q tests/streaming
```

The benchmark itself needs no host Python packages: all Python runs inside the
image built from the committed lock.

## PC setup (Windows 11 + Docker Desktop + WSL2, 32 GB)

1. Docker Desktop → Settings → Resources → WSL integration enabled for your distro.
2. `%UserProfile%\.wslconfig` (then `wsl --shutdown`):
   ```ini
   [wsl2]
   memory=20GB
   processors=8
   ```
3. Clone **inside the WSL filesystem** (e.g. `~/src`), not under `/mnt/c`, so bind
   mounts are fast and scripts keep LF line endings:
   ```bash
   git clone https://github.com/HuyyGiaa/ewallet-data-ai-platform.git ~/src/ewallet
   cd ~/src/ewallet && git checkout claude/flink-local-benchmark-20261031
   ```

Stack limits: Redpanda 2 CPU/4 GB (`--smp=2 --memory=3G`), JobManager 1 CPU/2 GB,
TaskManager 4 CPU/7 GB with 4 slots, tools 2 CPU/3 GB each. Ports bind to
`127.0.0.1` only: Flink UI `8081`, Redpanda Kafka `19092`, Redpanda admin `19644`.
Topic auto-creation is disabled; every run creates its own namespaced topics.

## Commands

```bash
scripts/streaming/flink_bench.sh prereq                 # docker, memory, ports, compose, uv lock
scripts/streaming/flink_bench.sh up                     # build image from uv.lock, start stack
scripts/streaming/flink_bench.sh functional baseline
scripts/streaming/flink_bench.sh functional optimized
scripts/streaming/flink_bench.sh perf baseline 500 120  # rate ∈ {100,500,1000,2000}, duration s
scripts/streaming/flink_bench.sh perf optimized 500 120
scripts/streaming/flink_bench.sh compare evidence/flink-benchmark/<baseline-run> evidence/flink-benchmark/<optimized-run>
scripts/streaming/flink_bench.sh down
```

Each run: restarts JobManager/TaskManager (no warm JVM/Python workers) → creates
fresh topics `ewallet.*.v1.<namespace>` (LogAppendTime) → submits the job in
`BENCHMARK_REPLAY` mode (fresh consumer group, `earliest` offsets, checkpoint dir
`file:///flink-data/checkpoints/<namespace>`) → publishes the bounded replay at the
target rate → waits for output quiescence and ≥1 checkpoint → collects metrics →
reads outputs → cancels the job. Runs refuse to start while any job is running.

Take Flink UI screenshots (job graph with backpressure, checkpoints tab,
TaskManager metrics) while the publish step is running.

## Replays

- Functional: 600 logical events, 5 s event-time spacing, 50 events/s, 8 users,
  6 merchants, 12 % late (30 min, beyond 2 min watermark + 10 min lateness),
  1.5 % duplicates, 3 malformed records → DLQ. Outputs are checked for exact-once
  per transaction, DLQ count, late bound, no retries, and every emitted online
  feature is recomputed by the pure-Python reference state (`--oracle`).
- Performance: `rate × duration` events, event-time spacing `1/rate`, 2000 users,
  500 merchants. Same spec → same payload SHA-256 for both profiles.

## Evidence (`evidence/flink-benchmark/<namespace>/`, git-ignored)

| File | Content |
|---|---|
| `run.json` | git SHA/dirty, profile, config SHA-256, image IDs, compose + uv.lock SHA-256, job id |
| `input/input_manifest.json` | spec, counts, payload SHA-256, target vs **achieved** send/ack rate |
| `metrics/snapshot_*.json` | Flink REST: vertices (records in/out, busy/backpressured/idle ms/s), checkpoints (duration, sizes, failures), job config, TaskManagers, consumer lag |
| `docker_stats.jsonl` | host `docker stats` CPU/RAM every 5 s |
| `outputs_summary.json` | per-topic counts, dispositions, latency p50/p95/p99, throughput, parity digests, functional checks |
| `flink_submit.log`, `flink_exceptions.json`, `flink_logs.txt` | diagnostics |

Definitions: processing latency = broker `LogAppendTime(accepted) − LogAppendTime(first raw)`
per transaction (Flink processing + sink delivery); it is not event lateness.
Consumer lag uses committed offsets, which Flink commits only on checkpoint
completion. Online feature records are emitted by event-time timers, so they
measure semantics, not latency, and the last ~12 min of event time stay pending
when the bounded input ends (identical for both profiles).

## Known limitations

- PyFlink job, image build and the stack were not executed on the laptop
  (resource guard); `tests/streaming/test_source_wiring_contract.py` is
  NOT_EXECUTED without the `flink` extra.
- The legacy feature operator rescans per-key history on each timer and parses
  JSON between operators; throughput is bounded by that logic for both profiles.
- With parallelism 4, per-subtask watermarks may classify borderline events
  differently from parallelism 1; parity digests report this instead of assuming it.
- Feast/Redis online writer, historical sink service and Silver bootstrap from the
  legacy branch are out of scope here; output stays in isolated Redpanda topics.
