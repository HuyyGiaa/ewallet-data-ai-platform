"""EDAI-1 DP2 Bronze -> Silver -> Core Gold. Requires a verified DP1 handoff; publishes dp2_handoff.json after Silver and Gold both pass.

One DagRun is one Spark profile (baseline or optimized). The task graph and all
logic come from data_platform/orchestration/airflow/edai1_pipeline.py and
ops/gcp/campaign_task.py. Runbook: docs/runbooks/edai1/cloud/edai1_campaign_runbook.md
"""

from data_platform.orchestration.airflow.edai1_pipeline import build_pipeline_dag


dag = build_pipeline_dag("dp2")
