# Flink Block A local benchmark — results (2026-10-31/11-01)

Local Docker benchmark on one WSL2 PC (Flink 1.19.1, Redpanda, 1 TaskManager × 4
slots). Baseline = parallelism 1, full checkpoints every 120 s; optimized =
parallelism 4, incremental RocksDB checkpoints every 30 s. Same image, same
input SHA-256 per workload. Raw evidence lives in the git-ignored
`evidence/flink-benchmark/<namespace>/` directories listed below.

## Status

| Gate | Result |
|---|---|
| FUNCTIONAL_VERIFIED | **YES** — functional replay, p=1 vs p=4, all four semantic digests identical, oracle 0 mismatches |
| PERFORMANCE_VERIFIED | **PARTIAL** — 100 eps: valid with semantic parity. 500 eps: measured, but semantic parity FAILS (see blocker) |
| Optimization verdict | **MIXED** — equal throughput at 100 eps with lower latency; at 500 eps optimized is faster but its output is not proven equivalent |
| BLOCKED | 500 eps semantic parity (event-time finalization under backlog) |

## Source-watermark correction after this report (runtime verification pending)

The job now assigns timestamps and the bounded-out-of-orderness watermark
strategy directly in `KafkaSource`, before Kafka splits are merged. Active
partitions therefore contribute independent split watermarks; production modes
retain configured idleness. `BENCHMARK_REPLAY` consumes only its isolated raw
topic because drain markers are published to every raw partition, while the
production modes continue to consume raw plus retry topics. Malformed and
non-event control records receive a no-timestamp sentinel and cannot advance a
watermark; benchmark envelopes remain rejected by production parsing and by the
production source timestamp assigner.

This is a code-level correction for the most probable 500 eps root cause, not
new performance evidence. All run IDs and failures below remain historical
evidence. Functional p=1/p=4 parity and repeated 500 eps baseline/optimized
semantic hashes must be collected again before changing the verdict.

## Root causes fixed in this campaign

1. **Scheduler-dependent lateness** (fixed by `0500e1f`, Codex). Functional replay
   classified 37 vs 49 events as TOO_LATE depending on when the periodic watermark
   fired. Benchmark replay now wraps each event in a `BENCHMARK_EVENT` envelope with
   an explicit logical watermark computed by the producer from replay order, and
   publishes a `BENCHMARK_WATERMARK` drain marker to every raw partition so every
   feature timer fires. `EventTimeDisposition` uses the logical watermark only in
   `BENCHMARK_REPLAY`; the production parser routes these envelopes to DLQ.
2. **State-iteration-order float drift** (`dc9a53e`). `ManagedHistoricalFeatures`
   read history from `MapState.items()` in unspecified order; float sums/stddev
   then differed by 1 ULP (`account_std_amount_30d`, 1 of 1795 records). History
   is now read sorted by its `event_ms:transaction_id` key. Regression test fails
   on the old operator and passes on the new one.
3. **Benchmark source idleness** (`f23d2cb`). Benchmark replay no longer marks
   splits idle (drain markers guarantee progress). Its commit message attributes the
   500 eps loss to idleness; the rerun **disproved** that as the main cause (see
   blocker). The change is kept because idleness is still scheduler-dependent in a
   bounded replay, and production modes keep idleness (unit-tested).
4. Runner: background `docker stats` samplers are killed on `die` as well
   (`6d2b1aa`, `fef6d0d`).

A deterministic benchmark harness is not proof that production event-time
scheduling is deterministic. Production watermark generation is unchanged.

## Tests

`docker run --rm -v $PWD:/workspace:ro -w /workspace -e PYTHONPATH=/workspace ewallet-flink-bench:local python -m unittest discover -s tests/streaming -t .`
→ **81/81 PASS** (79 previous + iteration-order + idleness-scope tests).
`uv lock --check` OK for all 7 uv projects.

## Runs at source commit `f23d2cb` (image `sha256:129253ce…` rebuilt from it)

Config SHA-256: baseline `bb986fdb…2a43`, optimized `4716db10…225f`.

| Workload | Profile | Namespace | Job ID |
|---|---|---|---|
| functional | baseline | `bench-func-baseline-20261031t165203z` | `619a75dc872ba0f095a3e44e12feddc0` |
| functional | optimized | `bench-func-optimized-20261031t165400z` | `e2c572b54c985ff145e546ffcb7d5fe5` |
| perf 100 eps | baseline | `bench-perf-baseline-100-20261031t165556z` | `bcb093493fcb565965aae3b844dab2ab` |
| perf 100 eps | optimized | `bench-perf-optimized-100-20261031t170149z` | `f428e89f3d6bcaa22bab985da2e46039` |
| perf 500 eps | baseline | `bench-perf-baseline-500-20261031t170535z` | `1ec92230a6a630030c8bf9fd7c932631` |
| perf 500 eps | optimized | `bench-perf-optimized-500-20261031t171455z` | `3b9c1621f4facf227db7f68de5d2aaa6` |

### Functional (612 raw events, input `82e0c9ab…e77d`)

Both: 600 accepted (528 ON_TIME / 72 TOO_LATE), 3 DLQ, 1795 online updates,
oracle 1795 compared / 0 mismatches, checkpoint completed. `accepted_sha256`,
`online_sha256`, `late_sha256`, `dlq_count` identical → compare exit 0.

### Performance, 120 s, 20× event-time speed-up (36 s warm-up, 84 s steady state)

| Metric | 100 baseline | 100 optimized | 500 baseline | 500 optimized |
|---|---:|---:|---:|---:|
| Producer achieved rate (eps) | 100.01 | 100.01 | 500.01 | 500.01 |
| Accepted / online updates | 12000 / 35975 | 12000 / 35975 | 60000 / **165624** | 60000 / **179356** |
| Semantic parity | **PASS** | | **FAIL (online)** | |
| Processing throughput (eps) | 92.86 | 93.58 | 130.75 | 217.70 |
| Accept latency p50/p95/p99 (ms) | 10829 / 14631 / 16188 | 5621 / 8020 / 9131 | 142562 / 328165 / 340037 | 93867 / 155832 / 163090 |
| Feature latency p50/p95/p99 (ms) | 51670 / 55707 / 56626 | 44063 / 48098 / 49753 | 224767 / 321222 / 322568 | 178653 / 197543 / 199635 |
| Max consumer lag (steady) | 9315 | 3179 | 47540 | 50855 |
| Max busy / backpressured ms/s | 758 / 533 | 665 / 0 | 866 / 1057 | 975 / 1070 |
| Checkpoints completed / failed | 1 / 0 | 4 / 0 | 1 / 0 | 2 / 0 |
| Checkpoint p50 / max (ms) | 6605 / 6605 | 4661 / 10185 | 16284 / 16284 | 49064 / 57256 |
| Max state size (bytes) | 4.9 M | 16.6 M | 15.3 M | 26.2 M |

Feature latency includes the designed event-time delay: an update is emitted only
after watermark passes T + 600 s allowed lateness (≈ 36 s wall at 20×). At 500 eps
neither profile keeps up (throughput < input rate); drain took 368 s (baseline)
vs 206 s (optimized). CPU/RAM samples: `docker_stats.jsonl` in each run dir
(two orphaned samplers from an earlier session were also polling `docker stats`
during these runs; minor host-load noise).

## Blocker: 500 eps semantic parity

At 500 eps baseline emits 13,732 fewer online updates and 2,744 shared updates
see one fewer history event; missing updates are concentrated in the last ~8
minutes of event time. Optimized itself is not stable at 500 eps (179,115 vs
179,356 updates across two runs). Most probable cause (not yet proven): watermarks
are assigned **after** the Kafka source merges 12 partitions
(`from_source(..., no_watermarks())` → `assign_timestamps_and_watermarks`), so
there are no per-partition watermarks; under backlog one partition can run ahead
of another by more than the 120 s out-of-orderness bound and timers fire before
lagging-partition records arrive. Fixing it means per-split source watermarks,
which changes production watermark behaviour and needs its own review.
Accordingly, 500 eps numbers are throughput evidence only, not an equivalence-
verified optimization result.

## Invalid / superseded runs (kept on disk)

- Superseded (pre-`f23d2cb`): `…t160807z`, `…t161038z`, `…t162141z`, `…t162414z`,
  `bench-perf-*-100-…t162640z/163043z`, `bench-perf-*-500-…t163451z/164408z`
  (the 163451z baseline overlapped a `uv lock --check` container). Their Redpanda
  topics were deleted to free partition memory; summaries remain on disk.
- INVALID: `bench-perf-optimized-100-20261031t165944z`,
  `bench-perf-baseline-500-20261031t170015z` (Redpanda partition memory limit),
  `bench-perf-optimized-500-20261031t170051z`, `bench-perf-baseline-500-20261031t172141z`
  (stopped by operator before job submit).

## Limitations

- Local single-host benchmark; does not prove GCP Feast Online / GKE Redis
  integration, Silver bootstrap or Cloud E2E.
- No screenshots captured in this session (owner will re-run for screenshots).
- Bench Redpanda (`--memory=3G`) holds ≈10 namespaces (6 topics × 12 partitions);
  delete old benchmark namespaces before new runs.
