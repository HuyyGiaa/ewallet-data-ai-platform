# Airflow cloud data orchestration

Airflow 3.3.1 exposes three manually triggered DAGs. Spark transformations run
in Dataproc Serverless; the scheduler only performs preflight checks, invokes
the bounded submitters, and verifies immutable Delta outputs.

| DAG | Responsibility | Output |
|---|---|---|
| `dp1_source_to_bronze` | Verify the source manifest, submit source-to-Bronze, verify eight Delta tables | Run-scoped Bronze root |
| `dp2_bronze_to_curated` | Verify Bronze, submit Silver and core Gold, verify 8 + 11 Delta tables | Separate Silver and core Gold roots |
| `dp3_historical_features` | Verify Silver and core Gold, submit four PIT feature tables, verify exact output | Feature Gold root |

All DAGs use `schedule=None`, `catchup=False`, and `max_active_runs=1`.
Submission tasks have no automatic retry because output paths are immutable.
Read-only validation tasks may retry once. Dataset version and every source and
output run ID are explicit DAG parameters.

`dataproc_ttl` is also explicit. It defaults to `2h` for bounded runs; the
canonical runbook uses `8h`. Submit tasks allow up to ten hours so Airflow does
not terminate the blocking `gcloud` process before the canonical batch TTL.

The former local daily `ewallet_batch_pipeline` DAG was removed. It embedded
developer-specific local Python paths and combined Silver, core Gold, and
historical features into one lifecycle.

## Bounded verification

The bounded replay used the already verified Dataproc outputs:

- Bronze: `r4-bronze-20261022-095608`
- Silver: `s1b-silver-configfix-20261022-174352`
- Core Gold: `s1b-gold-20261022-180212`
- Feature Gold: `s1b-features-20261022-181849`

Each DAG recorded a successful local Airflow DagRun. Submission tasks were
marked successful during replay to avoid rerunning paid workloads; all source
and Delta validation tasks executed against GCS and passed. The submitters had
already run the corresponding Dataproc batches successfully before replay.

Machine-readable evidence: [bounded Airflow verification](evidence/cloud/s1b_airflow_bounded_verification.json).
