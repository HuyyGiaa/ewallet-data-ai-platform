# Historical features bounded cloud pilot

> **Historical (superseded by the 5GB implementation, 2026-11-13).** This records a bounded DP3 run of the retired `r4-pilot-1gb-v1` pilot. It is kept for provenance and is not part of the current EDAI-1 workflow: use the [EDAI-1 DP1–DP3 pipeline runbook](edai1_campaign_runbook.md) for the `rehearsal-5gb-v1` campaign `ab-20261110225456`.

This evidence records the bounded DP3 historical feature run for dataset
version `r4-pilot-1gb-v1`. It is not the canonical 100 GB run.

- Source Silver run: `s1b-silver-configfix-20261022-174352`
- Source core Gold run: `s1b-gold-20261022-180212`
- Feature Gold run: `s1b-features-20261022-181849`
- Dataproc batch UUID: `919b6a09-1d80-4595-a45c-0c7e595a98f7`
- Spark transformation runtime: 199.56 seconds
- Result: PASS

The immutable output contains the user, account, device, and merchant
point-in-time feature tables. The merchant population is intentionally limited
to eligible payment transactions with a non-null merchant. Distinct entity and
timestamp snapshots explain why output counts can be lower than the Silver
transaction count.

All four persisted schemas exclude `label` and `fraud_type`, and Delta stats
show zero null entity keys and event timestamps. Six local PIT regression tests
also passed for strict history, same-timestamp, cold-start, and future-isolation
semantics.

Machine-readable evidence: [S1B historical feature pilot](../../../evidence/cloud/s1b_historical_features_bounded_pilot.json).
