# RAG Airflow runtime attempt 1 — missing GCS filesystem dependency

Result: **FAILED, preserved**.

- DAG: `rag_fraud_policy_ingestion` (real Airflow 3 `dags test`, isolated metadata DB)
- DagRun: `manual__2026-11-02T04:43:28.725139+00:00`
- Duration: 139.834 seconds
- Source HEAD at start: `50918fd74724994cc0e5660e1e3f821d7151203f`
- Backend isolation: existing GKE Redis, DB1 (DB0 untouched), unique RAG-owned
  GCS registry/publication prefixes
- DataHub mode: `plan_only` because the existing quickstart was stopped and
  local swap was already exhausted

Task outcome:

| Task | Outcome |
|---|---|
| `discover_documents` | SUCCESS |
| `parse_and_chunk` | SUCCESS |
| `generate_embeddings` | SUCCESS |
| `publish_feast_features` | FAILED after three Airflow attempts |
| `validate_online_readback` | NOT_EXECUTED (upstream failed) |
| `publish_datahub_lineage` | NOT_EXECUTED (upstream failed) |

Exact root cause:

```text
ModuleNotFoundError: No module named 'gcsfs'
ImportError: Please install gcsfs to access Google Storage
```

The task runtime declared Feast's GCP extra and used `fsspec` for `gs://`, but
its own locked dependency set did not include a GCS filesystem implementation.
No publication artifact, Feast registry, GCS object, or Redis DB1 row was
created before the failure. The complete Airflow console trace is preserved in
`airflow/dags-test.log`; generated source/chunk/embedding artifacts are retained
without alteration.
