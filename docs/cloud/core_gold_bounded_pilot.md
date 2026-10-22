# Core Gold bounded cloud pilot

This evidence records the bounded DP2 core Gold run for dataset version
`r4-pilot-1gb-v1`. It is not the canonical 100 GB run.

- Source Silver run: `s1b-silver-configfix-20261022-174352`
- Core Gold run: `s1b-gold-20261022-180212`
- Dataproc batch UUID: `5210f20f-ac58-44ca-ba6f-b548aab73ddd`
- Spark transformation runtime: 634.85 seconds
- Result: PASS

The immutable output contains five dimensions, three facts, one enriched OBT,
one 90-day user aggregate, and one merchant analytics table. Historical
feature tables are intentionally excluded because DP3 owns their lifecycle.

Delta transaction-log audit confirmed 3,700,000 rows in both
`fact_transactions` and `obt_transaction_enriched`. No core Gold schema
contains `label` or `fraud_type`; `fraud_labels` remains separate in Silver.

Machine-readable evidence: [S1B core Gold pilot](../evidence/cloud/s1b_core_gold_bounded_pilot.json).
