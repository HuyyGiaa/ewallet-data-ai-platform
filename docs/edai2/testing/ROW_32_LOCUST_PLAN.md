# Row 32: Locust against real backends

Final row 32 evidence must come from deployed backends. The tooling is implemented and works against real local services. **No GKE run has happened**, so the row is not complete.

Row 32 asks for a load test of the data-retrieval Web API, so the Feature API is the primary target. Fraud inference is measured as a second target.

| Target | Path |
|---|---|
| `feature` | Locust → Feature API → Feast SDK → Redis → response |
| `inference` | Locust → Fraud API → Feast → Redis → assembly → KServe → prediction |

## Harness

| Part | File |
|---|---|
| Targets and response contracts | [`tests/load/real_targets.py`](../../../tests/load/real_targets.py) |
| Locust user | [`tests/load/locustfile_real.py`](../../../tests/load/locustfile_real.py) |
| One run, with manifest and reports | [`scripts/testing/run_real_load.sh`](../../../scripts/testing/run_real_load.sh) |
| Metrics summary | [`scripts/testing/real_load_report.py`](../../../scripts/testing/real_load_report.py) |
| Pinned Locust environment | [`infra/load-test`](../../../infra/load-test/pyproject.toml) |
| Real entity fixtures | [`ops/experiments/fraud_entities.py`](../../../ops/experiments/fraud_entities.py) |

The B1 mock-backed harness stays as it is, for isolated tests. These files sit beside it and change nothing on B1, so the earlier plan to edit `tests/load/payloads.py` is no longer needed.

A request counts as a success only when it honours its response contract. Only valid requests are sent. Invalid-request and fault scenarios stay in the B1 harness.

## Fixtures and freshness

The API rejects features more than 10 minutes older than `event_timestamp`, and online rows keep their own times, so one fixed timestamp fits almost no entity.

```text
build    real entity ids from the training snapshot
prepare  read each entity's view times, value-free; set event_timestamp just after the newest
verify   send each entity once to the deployed API; keep only 200s, and keep the counts
```

An entity whose views lie more than 10 minutes apart is dropped. Freshness is never relaxed. **Risk:** on real data few entities may qualify. If too few remain, a fresh materialization is a separate owner decision.

## Workload

| Profile | Users | Spawn rate | Duration |
|---|---:|---:|---:|
| low | 5 | 2/s | 5 min |
| medium | 10 | 5/s | 5 min |
| high | 20 | 5/s | 5 min |

Each run writes the HTML report, statistics, history, failures, terminal log and a manifest with source SHA, image digest, model version, fixture hash and timestamps. The network path (`internal`, `external`, `local`) is part of the output directory, so different paths never merge.

## Local harness check

Real Feature API, Fraud API, KServe predictor, Feast and Redis on a laptop, with fixture features, 5 users for 30 seconds. **This proves the tooling only.** See [the summaries](../../../docs/evidence/edai2/load/local_harness_20261107T065613Z).

| Target | Requests | Failed | Req/s | p95 |
|---|---:|---:|---:|---:|
| feature | 9,651 | 0 | 332.6 | 20 ms |
| inference | 1,473 | 0 | 50.8 | 130 ms |

`prepare` and `verify` both kept 300 of 300 fixture entities. Real data will not be this clean.

The GKE procedure is in [the Session A packages](../../runbooks/edai2/SESSION_A_APPROVAL_PACKAGES.md).
