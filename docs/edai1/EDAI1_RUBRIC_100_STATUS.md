# EDAI-1 rubric status

Scoring is evidence-gated, not source-presence-gated. V5 raises the locally verified subtotal from 31/77 to **37/77** by publishing and reading back physical lineage for DP1, DP2 and DP3 (+6). On the full 100-point rubric this is 37/100; excluded owner rows are not silently removed from the official denominator.

| Rows | Points | Status | Evidence / gate |
|---|---:|---|---|
| 3–4 | 5 | VERIFIED_LOCAL | Compose smoke; optimized image A/B |
| 5 | 2 | BLOCKED_CLOUD | skew/cardinality cloud scan requires cost-safe bounded run |
| 6–8 | 6 | VERIFIED | preserved immutable generator/schema/duplicate/storage evidence |
| 9–10 | 4 | OWNER_DEPENDENCY | Flink/Block A untouched |
| 20 | 3 | VERIFIED | bounded compaction proof; cloud repeat blocked |
| 21 | 3 | VERIFIED_LOCAL | 2M rows, 7 reps, equality gate |
| 22–27 | 12 | EVIDENCE_READY, 0 CLAIMED | logs recovered; authoritative Airflow metadata absent; PIT owner dependency |
| 28,30,32 | 6 | VERIFIED_LIVE_LOCAL | 42/42 live GMS aspects, 31 exact physical edges, UI screenshot |
| 29,31,33 | 6 | SOURCE_READY, 0 CLAIMED | validation logs exist; DataHub assertion result aspects missing |
| 34 | 2 | SOURCE_READY | complete 8/8/8/15 inventory and schema document |
| 35–36 | 4 | OWNER_DEPENDENCY | Spark schema work excluded |
| 37–38 | 4 | VERIFIED | relationships and naming evidence preserved |
| 39 | 10 | VERIFIED | deterministic sharding + immutable manifest validation |
| 40 | 10 | OWNER_DEPENDENCY | PIT/Feast publication not claimed |

No score is awarded merely for a dry run, generated plan, or historical log without the required runtime identity and readback.

