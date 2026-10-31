# Runtime blockers

| Gate | Exact blocker | Continued work |
|---|---|---|
| Paid cloud benchmark | Billing report shows 2,321,891 VND usage through 29 Oct, but reporting is delayed and a live GKE node/disks/network continue accruing. No defensible upper bound for unreported liability exists. | local tests, live local DataHub, Docker, docs and reusable tooling completed |
| Cloud Delta A/B | current harness may clone a full canonical table; bounded subset path is not yet implemented | local compaction proof retained; cloud run not attempted |
| Airflow scoring UI | metadata DB has zero authoritative historical DagRuns/TIs; rerun forbidden | 36 immutable logs hashed and indexed |
| DataHub validation rows | lineage was published; run-bound assertion aspects were not | exact remaining aspect contract documented |
| SCD2 / feature `created` | requires Spark/DP2/DP3 changes, explicitly excluded | owner proposal retained |
| PIT / Feast | separate MLOps owner; registry overwrite forbidden | no claim or mutation |

