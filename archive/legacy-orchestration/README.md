# Retired EDAI-1 orchestration (2026-11-11)

Kept for audit only. Nothing here is loaded by Airflow or meant to be run: the DAG files are
outside the DAG folder and the scripts are not executable (their relative paths no longer
resolve from this directory). The current workflow is three independent DAGs driven by
`scripts/edai1/*.sh`; see the [EDAI-1 DP1–DP3 pipeline runbook](../../docs/runbooks/edai1/cloud/edai1_campaign_runbook.md).

The files are copies of `main` at `f4ce0b9ec4c298ba640eb22a2c2a516790d8208a`, the last commit
that ran them; `git log --follow` on each path shows the earlier history.

| File | Was | Why retired |
|---|---|---|
| `dags/edai1_data_pipeline_campaign.py` | Integrated campaign: DP1–DP3 as TaskGroups of one DagRun per profile | Replaced by three independent DAGs with immutable handoff manifests. Its one DagRun (`manual__2026-11-10T20:41:14.355396+00:00`, benchmark `ab-20261111033139`, Baseline) stopped at `dp1_bronze.wait_quota` and is abandoned; the DagRun, its logs and the Airflow metadata are kept, and that benchmark is never reused |
| `dags/dp1_source_to_bronze.py` | DP1 with a pair manifest, canonical defaults | Same DAG ID now runs the handoff workflow |
| `dags/dp2_bronze_to_curated.py` | DP2 Method 2: Silver and Gold A/B in one DagRun with winner selection | No winner selection; one profile per DagRun |
| `dags/dp3_historical_features.py` | DP3 on explicit Silver/Gold run IDs and a pair manifest | Reads a verified DP2 handoff instead |
| `scripts/run_dp1_canonical.sh`, `run_dp2_canonical.sh`, `run_dp3_canonical.sh`, `run_dp2_method2.sh`, `lib/spark_ab_common.sh` | Trigger helpers for the DAG versions above | Their configurations do not match the new DAG parameters |

The historical 100GB evidence under `docs/evidence/` was produced by these versions and is
unchanged. The modules they called (`ops/gcp/dp2_method2.py`, `ops/gcp/recover_dp2_method2.py`,
the stage-pair tools) stay in `ops/gcp/` for evidence recovery and their own tests.
