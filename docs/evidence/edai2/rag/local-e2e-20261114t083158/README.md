# RAG ingestion — local end-to-end run of 2026-11-14

DagRun `manual__rag-local-e2e-20261114t083158` of `rag_fraud_policy_ingestion`.
It ran in an isolated Airflow 3.3.1 (`scripts/llm_agent/rag_isolated_airflow.sh`)
from source commit `1e6a88a2` (`airflow/git_sha.txt`), and was triggered and
collected with `scripts/llm_agent/rag_runtime_run.sh trigger`. These are the
run's own artifacts, unchanged (`SHA256SUMS`). The staged corpus copy
(`source/`) is omitted because it is identical to `data/llm_agent/rag/fraud-policy-v1/`.

| Component | Used |
|---|---|
| Embeddings | Real `sentence-transformers/all-MiniLM-L6-v2@1110a243…` ONNX model |
| Feast | 0.66 with the Redis Online Store: a local `redis:7.4.2-alpine` container (`noeviction`), DB 3, project `ewallet_rag_verify`, local registry and Parquet |
| DataHub | Local quickstart v1.7.0.1 (GMS 127.0.0.1:18080) |
| Cloud | None. No GKE, GCS or cloud Redis was used |

| File | Result |
|---|---|
| `airflow/task_states.json` | 6/6 `success` |
| `documents.json`, `chunks.json`, `embeddings.json` | 2 documents, 0 checksum mismatches; 4 deterministic chunks; 4 × 384 vectors, 0 invalid |
| `publication.json` | 4 rows, identity `b85ae796…`, offline `REUSED`, Redis DB 3 `OWNED` (claimed by the run before) |
| `validation.json` | `MATCH`, recall@k 1.0, MRR 1.0, 0 citation errors, ownership `MATCH` |
| `datahub.json` | `PUBLISHED_AND_READ_BACK`: 27 aspects read back, 5/5 assertions passed, search and lineage index `VISIBLE` |
| `feast_registry.json`, `online_readback.json`, `datahub_raw_readback.json`, `summary.txt` | Independent read-only views written by `collect` |

This is local verification, not a GKE deployment, and it includes no
screenshots. See [RAG_PIPELINE.md §11](../../../../edai2/rag/RAG_PIPELINE.md#11-verification-results).
