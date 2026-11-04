# G3 functional RAG ingestion — 2026-11-02

## Verdict

- `RAG_AIRFLOW_DAGRUN=PASS`: a real isolated Airflow 3 DagRun executed all six tasks successfully.
- `RAG_FEAST_PUBLICATION=PASS`: four deterministic chunk rows were written to immutable GCS paths and materialized into the isolated Redis database 1; online readback matched the offline publication.
- `RAG_RETRIEVAL=PASS`: direct retrieval from the newly materialized DB1 returned versioned citations with `recall@k=1.0`, `MRR=1.0`, and zero citation errors.
- `RAG_DATAHUB_LINEAGE=BLOCKED`: the task constructed 25 metadata records and passed all five local governance checks, but ran in `plan_only`; no current DataHub publication or independent readback occurred.
- `FULL_BUSINESS_E2E=BLOCKED`: the Coordinator still prints a pseudo-tool invocation instead of calling its registered A2A tool. The RAG result must not be represented as a successful Coordinator end-to-end run.

## Provenance

- Source HEAD at run start: `f265f2e7ae36ddd2f201327b92d045677fc3896a`
- Logical run label: `g3-functional-rag-20261102t045700z`
- Airflow DAG ID: `rag_fraud_policy_ingestion`
- Airflow run ID: `manual__2026-11-02T04:47:47.888480+00:00`
- Airflow duration: `23.189399` seconds
- Corpus: `fraud-policy-v1-20261030`
- Feature-index revision: `fraud-policy-feast-v1`
- Publication identity: `b85ae796221aa0b9e2adfd7ff19488f612dff18c8d60c3c44f0134b7266bf549`

The Airflow run used a throwaway metadata database and the repository's locked `rag-ingestion` environment. It did not deploy or alter the canonical Airflow service. The runtime connection file is deliberately outside Git at `~/.config/ewallet-rag/runtime.env` with mode `0600`; it contains no source-of-truth data and must never be committed.

## Real runtime results

All six task states in `airflow/task_states.json` are `success`:

1. `discover_documents`
2. `parse_and_chunk`
3. `generate_embeddings`
4. `publish_feast_features`
5. `validate_online_readback`
6. `publish_datahub_lineage`

Publication produced two source documents, four deterministic chunks, four 384-dimensional finite embeddings, and four isolated online rows. The offline parquet and registry were written to new immutable run-scoped paths:

- `gs://ewallet-data-ai-platform-hg26-data/features/rag/runs/g3-functional-rag-20261102t045700z/fraud-policy-feast-v1/chunks.parquet`
- `gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/runs/g3-functional-rag-20261102t045700z/registry.db`

`gcs-readback.json` records the remote object generations and checksums. `redis-db1-readback.json` records four hashes in isolated DB1. Canonical ML Feast data and production RAG DB0 were not overwritten.

## Retrieval correlation boundary

`direct-knowledge-search.json`, `direct-chunk-lookup.json`, and `validation.json` read the newly materialized DB1 publication directly. `current-mcp-knowledge-search.json` is a separate official-MCP-SDK call against the currently deployed production Feature-RAG MCP server. It returned the same corpus revision, chunk IDs, embedding revision, source checksums, and near-identical scores.

This is strong semantic-identity correlation, but it is **not** proof that the production MCP process read DB1: the current production server still uses its pre-existing DB0 registry. Promotion/cutover was outside this bounded ingestion run.

## DataHub boundary

`datahub.json` contains 25 constructed metadata records and five passing quality checks. Its authoritative status is `PLAN_ONLY_NOT_PUBLISHED`. The existing local DataHub stack was stopped and local swap was already exhausted, so starting another stack would have violated the resource-safety gate. No claim of current DataHub runtime publication or readback is made.

## Reproduction

From this worktree, with the private runtime environment file prepared and the locked project synchronized:

```bash
uv lock --check --project infra/docker/rag-ingestion
uv sync --locked --project infra/docker/rag-ingestion
RAG_ISOLATED_ONLINE_STORE=redis \
EWALLET_RAG_ENV_FILE="$HOME/.config/ewallet-rag/runtime.env" \
scripts/validation/run_rag_dag_isolated.sh \
  "$HOME/miniconda3/envs/airflow/bin/airflow" \
  <new-immutable-run-label> plan_only
```

Use a new run label and new GCS paths. Use an isolated Redis database/keyspace. Do not reuse this run's immutable GCS destinations and do not point the script at canonical ML Feast keys.

## Evidence map

- `airflow/dags-test.log`: complete real DagRun trace (including the six task executions).
- `airflow/dag_runs.json`, `airflow/task_states.json`: Airflow metadata readback.
- `documents.json`, `chunks.json`, `embeddings.json`: deterministic pipeline artifacts.
- `publication.json`, `gcs-readback.json`, `redis-db1-readback.json`: publication and independent storage readback.
- `validation.json`: retrieval metrics and cited-source validation.
- `direct-knowledge-search.json`, `direct-chunk-lookup.json`: direct reads against the new isolated publication.
- `current-mcp-knowledge-search.json`: current deployed MCP regression/correlation call.
- `datahub.json`: plan-only metadata and assertions; not runtime publication evidence.
- `SHA256SUMS`: integrity manifest for this evidence directory.
