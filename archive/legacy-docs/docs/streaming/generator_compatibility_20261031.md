# Streaming generator compatibility — 2026-10-31

## Provenance and current schema

- Integration base: `1e08659957b902f766af9cc08d560bd024b73c76`.
- Offline generator implementation: `data_platform/generation/src/offline/offline_generator.py`, latest source commit `55c1e0b08ad4002e0a17aa991cf52e51cc213ddd` (`feat: improve fraud generator behavioral realism`).
- Immutable sharded publisher wrapping that generator: latest source commit `bff5316` (`feat: publish immutable source datasets to GCS`).
- Dataset manifest schema: integer version `1`.
- Physical transaction schemas: `transactions_v1` (no `channel`) and `transactions_v2` (adds `channel`).
- Canonical downstream reference: `data_platform/contracts/silver_transactions.yml`, contract version `1`; historical V1 channel is normalized to `UNKNOWN`.
- Streaming source recovered narrowly from `codex/streaming-block-a` at `3fa7c483d9e1596fb2ebd06b931403a38680b51b`; the dirty source worktree was read but not modified.

The generator code at the consolidated checkpoint is the authoritative domain implementation. A committed dataset manifest was not present in this repository, so this change does not claim that `55c1e0b` is the execution SHA of any particular cloud dataset. It is the exact latest commit that changed the offline generator and configuration available at the requested stable checkpoint.

## Compatibility changes

| Area | Old streaming generator | Updated generator |
|---|---|---|
| Entity references | Independent modulo IDs could mismatch user/account/device | Deterministic catalog enforces account and device ownership; merchants and counterparties exist |
| Domain values | Used invalid channel `mobile` | Reuses current enums: `app`, `web`, `atm`; V1 adapter maps missing channel to `UNKNOWN` |
| Transaction behavior | Always payment; balances ignored amount | Uses current transaction weights and valid deposit/debit balance semantics |
| Schema evolution | No offline adapter | Explicit V1/V2 adapter and V2 export shape |
| Fraud labels | Unspecified | Fraud ground truth is never emitted; it remains a separate offline dataset |
| Time | Event/arrival fields existed but were not mapped | `event_time -> timestamp`; `arrival_time -> ingested_at`; adapter explicitly assigns documented UTC to the offline generator's naive datetimes |
| Problem simulation | Helpers only | Exact bounded targets: late 12%, duplicate 1.5%; out-of-order helpers preserved |
| Load behavior | Logical data and sleeping publisher coupled | Pure logical sample; realistic rate is separate from optional 30x stress rate |
| Reproducibility | Stable IDs for events only | Seed 42 defaults, stable transaction/entity IDs, deterministic scenario selection |

Streaming envelope version remains `1.0` because the existing Flink `ParseAndValidate` input reads that exact field set. This is distinct from physical offline schema V1/V2 and manifest schema version 1.

## Event shape and sample

The envelope contains the offline transaction fields using the existing streaming names (`transaction_type`, `event_time`, `arrival_time`) plus `event_id`, `trace_id`, and `schema_version`. `TransactionEvent.to_offline_record()` maps it to the exact 15-column current V2 transaction shape. The fixed-seed sample is in `examples/streaming/generator_sample_seed42.jsonl` and can be reproduced with:

```bash
python3 -m data_platform.streaming.generator.cli \
  --count 3 --seed 42 --rate 1 \
  --start-time 2026-05-01T12:00:00+00:00
```

`--rate` changes the planned spacing only. `--stress` applies the retained multiplier of 30 without changing entity identities, transaction values, or scenario selection.

## Focused validation

Command:

```bash
python3 -m unittest tests.streaming.test_generator_compatibility -v
```

Result: 10/10 focused tests passed. Coverage includes offline V2 shape and types, existing Flink JSON parsing, referential integrity, fraud-label isolation, determinism, duplicates, late arrivals, exact target rates, stress-rate separation, and V1/V2 schema evolution. `compileall`, fixed-sample byte comparison, and `git diff --check` also passed. No Flink, Redpanda, Spark, Docker, Kubernetes, cloud workload, or historical dataset generation was started.

Two pre-existing broader generator suites were also attempted but were not runnable in the host `python3`: `tests.test_fraud_generator` requires `pyarrow`, and `tests.unit.test_sharded_generator` requires `faker`. No dependency installation was performed for this narrow task. These missing optional test dependencies do not affect the dependency-free focused compatibility suite.

## Remaining integration issues for Flink

1. Claude should cherry-pick this task commit into its isolated Flink integration branch and resolve against the dirty Block A versions of `events.py` and `scenarios.py`; those files overlap intentionally.
2. The existing broker publisher/CLI in Block A was not copied because it couples this narrow task to Redpanda. Its publisher can consume `TransactionEvent` unchanged after integration.
3. This task validates the input contract statically through the same `TransactionEvent.from_json` path. Runtime watermark, broker throughput, and Flink state behavior remain outside this task and require the later isolated benchmark.
4. The 31 hybrid features and 41 final inference inputs remain downstream contracts; the generator correctly emits neither feature vectors nor fraud labels.
