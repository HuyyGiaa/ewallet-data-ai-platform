# rag_integration-20261101

**Status level: INTEGRATION TEST_PASS (source + local).** No live-Airflow, GKE Redis or MCP runtime is claimed here.

| Item | Value |
|---|---|
| Integration branch | `integration/rag-canonical-20261101` (worktree `.worktrees/rag-canonical-20261101`) |
| Canonical contained | `feature/phase2-fraud-platform` @ `2097059` (0 behind; canonical can fast-forward) |
| RAG source | `feature/airflow-rag-integration` merged with ordinary `--no-ff` merges, no conflicts |
| uv locks | all 8 projects `uv lock --check` PASS; RAG runtime 161 packages, uv 0.13.5 |
| RAG tests (rag-ingestion env) | 57 passed, 1 opt-in skip |
| MCP tests (llm-agent-tooling env) | 90/91; the 1 error is pre-existing (`exceptiongroup` import on Python 3.12, same at `fb8cab8`) |
| tests/llm_agent (Airflow env) | 336 run, 1 pre-existing error (`test_vllm_model_image` needs `tomllib`, Python 3.11+, same at `fb8cab8`) |
| tests/unit | 24 failures/errors, all also present at baseline `fb8cab8` (`tests_unit_failures_*.txt`), 0 new |
| Real Airflow DAG parse | 0 import errors |
| Jenkinsfile.rag | every non-trigger stage run with `/bin/sh -e` on a clean archive (`jenkins_stage_simulation.log`); a failing stage exits 1. Not a Jenkins run. |
| Trivy 0.75.0 on the RAG lock | 1 HIGH (filelock CVE-2026-112940, fix needs Python 3.11); reviewed, expiring exception; scan then exits 0 |
| Clean-checkout reproduction | isolated Airflow `dags test` + real local DataHub from a `git archive`: 6/6 success, DataHub 25/25 MATCH, Feast readback MATCH |
| Runtime preflight vs live Airflow | `runtime_preflight.txt`: lists the remaining operator/G1 blockers |
