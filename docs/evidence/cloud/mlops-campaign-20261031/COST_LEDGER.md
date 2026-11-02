# MLOps campaign cost ledger (2026-10-31)
- Billing account: 0194C5-97FAAA-4CD848 (open, linked to ewallet-data-ai-platform-hg26)
- Owner-observed Free Trial credit at campaign start (2026-10-31 ~22:15 ICT): >= 3,000,000 VND (owner answer, console)
- Prior verified UI reading: 5,904,897.19 VND remaining (2026-10-29)
- Campaign hard cap: 1,000,000 VND; minimum reserve kept: 2,000,000 VND
- Start state: GKE ewallet-dev cpu-pool 0 nodes; no Dataproc/Cloud Run running
- 2026-10-31T15:31:15Z scale cpu-pool 0->2 (e2-standard-4) for Redis/MLflow/KFP; must return to 0 at campaign end
- 2026-10-31T15:33:43Z resize REFUSED: CPUS_ALL_REGIONS quota 32 fully used by Dataproc batch; no nodes created; retry after batch
- 2026-10-31T16:32:34Z Cloud Run job ml2-mat-261031163234 4vCPU/16Gi timeout 90m retries 0 (~0.67 USD ceiling)
- 2026-10-31T16:32:50Z scale cpu-pool 0->2 (retry after Dataproc)
