# rag_local-airflow-dagstest-20261101

**Status level: TEST_PASS (local orchestration). NOT RUNTIME_VERIFIED.**

The real Airflow 3.3.1 interpreter (conda env `airflow`, Python 3.10.20) ran every task of
`rag_fraud_policy` with `airflow dags test` in a throwaway `AIRFLOW_HOME` and SQLite
metadata DB (`scripts/validation/run_rag_dag_isolated.sh`). The shared Airflow, its metadata
DB, GKE, Redis and DataHub were not touched.

| What ran | Value |
|---|---|
| Params | `online_store=sqlite` (local-dev Feast profile), `datahub_mode=plan_only`, `run_label=local-airflow-dagstest-20261101` |
| Tasks | 6/6 `success` (`airflow_dags_test.log`), DagRun `state=success` |
| Corpus | 2 documents, `fraud-policy-v1-20261030`, checksums verified (`documents.json`) |
| Chunks | 4, deterministic ids (`chunks.json`) |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, ONNX, 384-d, L2-normalised (`embeddings.json`) |
| Feast | parquet → `apply` → `materialize` into a temporary SQLite online store, 4 rows (`publication.json`) |
| Readback / retrieval | 4/4 rows `MATCH`, recall@2 = 1.0, MRR = 1.0, 0 citation errors (`validation.json`) |
| DataHub | `PLAN_ONLY_NOT_PUBLISHED`: 25 records and 5 passing quality checks were generated, **nothing was published** (`datahub.json`) |
| DAG parse | `dag_isolated_parse.json` |
| uv | `uv_locked_install_and_tests.txt` |

Feast paths in `publication.json` point to the deleted temporary home. Canonical evidence
needs the shared Airflow, `online_store=redis` and `datahub_mode=publish` with readback
(see `docs/llm_agent/phase_c_rag.md`).
