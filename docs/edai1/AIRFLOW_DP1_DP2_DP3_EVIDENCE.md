# Airflow DP1/DP2/DP3 evidence

The collector read existing logs only; it did not start Airflow or rerun a DAG. It recovered 36 task logs across seven historical runs: 35 terminal successes and one preserved failure. The failure is retained to show the actual recovery chain rather than rewriting history.

Evidence: `artifacts/airflow_dp1_dp2_dp3_evidence.json` in the V5 run directory. Each entry records its path, SHA-256, terminal marker and referenced Dataproc batch ID. This proves the historical task output but does **not** recreate missing Airflow metadata. Rows 22–27 therefore remain source/evidence-ready, not fully claimed: the metadata database currently contains no authoritative DagRun/TaskInstance state and rerunning the 100 GB pipelines is prohibited.

