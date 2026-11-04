# Phase C — Versioned fraud-policy RAG

Status (2026-11-01): the Airflow ingestion DAG is implemented and tested; see
"Airflow-orchestrated ingestion" below. It has run end to end only under an
isolated local `airflow dags test` (sqlite Feast profile, DataHub plan-only).
The canonical run (shared Airflow, Feast Online Redis, DataHub) has not run.
The sections in between are kept as the design history.

## Frozen flow

`text ingestion → checksum validation → bounded chunking → pinned embedding →
Feast publication artifact → retrieval/citations/evaluation → DataHub lineage plan`

The corpus manifest preserves `document_id`, `chunk_id`, source URI/checksum,
corpus revision, embedding model revision, feature/index revision, event time and
publication time. Evidence artifacts are create-only so a retry cannot silently
overwrite a prior run.

The embedding model is `sentence-transformers/all-MiniLM-L6-v2` at immutable
revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Runtime calls use an
OpenAI-compatible `/v1/embeddings` endpoint with a file-mounted API-key
reference; secrets are not accepted as command-line values.

## Feast compatibility decision

The pinned Feast runtime is 0.66.0. Its type system can represent embeddings as
`Array(Float32)` and the feature definition includes cosine vector-index
metadata. Vector similarity search is experimental and online-store-specific.
The currently configured Redis store is therefore classified as
`PUBLICATION_ONLY_RUNTIME_BLOCKED`, not as a vector database. Source tests use
bounded exact cosine retrieval over the immutable publication artifact; this is
not evidence of live Feast vector retrieval.

An approved runtime gate must either select and validate a Feast-supported
vector online store or explicitly approve the bounded exact-retrieval topology.
It must then capture Feast apply/materialization/readback and correlated query
evidence.

## Evaluation and governance

The versioned evaluation set records expected document identities and produces
recall@k, mean reciprocal rank, empty-retrieval rate and citations containing
source checksum plus all corpus/model/index revisions. DataHub output is a
lineage plan only and requires `upstreamLineage` readback before runtime credit.

The manual Airflow DAG builds and evaluates create-only artifacts, then verifies
their shared identities and the explicit `runtime_verified=false` gate. Actual
Feast and DataHub publication remain separate approved operations.

## Retrieval over Feast Online (added 2026-10-31)

Status: **SOURCE_READY + TEST_PASS. Runtime blocked** on materialized Feast
Online rows and an embedding endpoint. The previous status was
PUBLICATION_ONLY_RUNTIME_BLOCKED. It is now
PUBLICATION_AND_EXACT_RETRIEVAL_SOURCE_READY.

Storage and search are kept separate:

| Concern | Implementation | Claim |
|---|---|---|
| Storage | Feast `fraud_policy_chunks` (Redis online store): text, provenance, `Array(Float32)` embedding, read by `chunk_id` | Redis key lookup, which Feast+Redis supports |
| Search | `FeastOnlineExactRetriever`: fetches exactly the canonical chunk set (`TrustedChunkIndex`), then in-process cosine (`retrieve_exact`) | No Feast/Redis vector search is claimed |

- Each row must match its canonical chunk (text, document, source checksum)
  and the manifest corpus, embedding and index revisions. Otherwise the
  result is `rag_index_provenance_mismatch`. A missing or expired row gives
  `rag_index_not_materialized`, an outage gives `rag_index_unavailable`, and
  an unknown or extra chunk gives `rag_index_contract_error`. Partial results
  are never served.
- Query embedding: an OpenAI-compatible endpoint that serves the manifest
  model (`all-MiniLM-L6-v2` at revision `1110a243…`, 384 dimensions). The
  retriever refuses an embedder with another revision or dimension.
- Scale limit: O(chunks) per query, bounded by `max_chunks` (2000). The
  coursework corpus has 2 documents. A larger corpus needs an approved
  vector-search backend.
- Opt-in: `EWALLET_RAG_RETRIEVER=feast-online-exact` plus
  `EWALLET_RAG_EMBEDDING_ENDPOINT`. The default remains the fail-closed
  placeholder, and an incomplete configuration fails at startup.
- Tests: `tests/llm_agent/test_rag_online_retriever.py`, which uses a
  deterministic vector fixture, not a real embedding model. Two targeted
  mutants cover the provenance and materialization checks.

Runtime prerequisites (not run; DP2 active on 2026-10-31):
1. Run the RAG DAG to produce the publication parquet with real embeddings.
2. `feast apply` + `materialize` into the online Redis.
3. Provide an embedding endpoint for the pinned model.
4. Recompute Recall@k and MRR with real embeddings
   (`ops/llm_agent/run_rag_evaluation.py`).

## Airflow-orchestrated ingestion (added 2026-11-01)

Rubric edai-2 R9/R10 (RAG Data Pipeline + governance), LD21.316 addenda 3–4.
Status: **SOURCE_READY + TEST_PASS**. Not RUNTIME_VERIFIED.

### DAG `rag_fraud_policy`

| Task | Stage (`ops.llm_agent.rag_ingest`) | Output in `docs/evidence/llm_agent/rag_<run_id>/` |
|---|---|---|
| `discover_documents` | stage the corpus (repo directory or `gs://` prefix), verify manifest SHA-256 | `documents.json`, `source/` |
| `parse_and_chunk` | whitespace-normalised word chunks (80/16), chunk ids `<document_id>:<ordinal>`, chunked twice to prove determinism | `chunks.json` |
| `generate_embeddings` | pinned `all-MiniLM-L6-v2@1110a243…` ONNX export, mean pooling + L2, 384-d | `embeddings.json` |
| `publish_feast_features` | immutable parquet per `feature_index_revision` → `feast apply` (partial) → `materialize` | `publication.json` |
| `validate_online_readback` | `get_online_features` for every canonical chunk id, provenance check, top-k retrieval on the evaluation set, citation check | `validation.json` |
| `publish_datahub_lineage` | DataFlow/DataJobs, corpus → Feast lineage, schema, five quality assertions; each aspect read back | `datahub.json` |

- Manual (`schedule=None`), `max_active_runs=1`, `retries=2` with a 1 minute delay, and
  a per-task `execution_timeout`. Failures propagate because the tasks run in sequence.
- Params: `source_location`, `online_store` (`redis` canonical | `sqlite` local-dev),
  `datahub_mode` (`publish` | `plan_only`), `run_label`. JSON-schema validated and
  templated only into env values. The run id defaults to the sanitised Airflow run id.
- Idempotent. A completed artifact is never overwritten and a failed attempt
  leaves none. Re-publishing the same revision reuses the identical parquet
  (`REUSED`) and upserts the same online keys. A different payload under the
  same revision fails.
- Credentials are never in params or source. They come from the worker env or
  from a private `KEY=VALUE` file (`EWALLET_RAG_ENV_FILE`, default
  `~/.config/ewallet-rag/runtime.env`, mode 600, allowlisted keys). The file
  lets the settings change without restarting the shared scheduler.

### Embedding

The manifest's pinned revision ships `onnx/model.onnx` and `tokenizer.json`. Embedding uses
onnxruntime + tokenizers (`ai_platform/llm_agent/rag/embedding.py`), with no torch and no
embedding service. Loading fails unless `modules.json` is Transformer → mean Pooling →
Normalize. The `rag-embedding` service referenced by the earlier DAG never existed.
The same embedder serves query time in feature-rag-mcp through
`EWALLET_RAG_RETRIEVER=feast-online-exact` + `EWALLET_RAG_EMBEDDER=local-onnx`.

### Feast

The `fraud_policy_chunks` schema is unchanged. `build_rag_feature_objects(path)` only
binds the FileSource to the run's parquet.
- `redis` profile: the existing Feast Online Redis (`EWALLET_REDIS_CONNECTION_STRING`) with a
  RAG-owned registry (`EWALLET_RAG_FEAST_REGISTRY`, project `EWALLET_RAG_FEAST_PROJECT`, default
  `ewallet_rag`). The MLOps-published online registry of the 31 historical features is never
  written; feature-rag-mcp opens the RAG registry on the same Redis connection when
  `EWALLET_RAG_FEAST_REGISTRY` is set. `EWALLET_RAG_PUBLICATION_BASE` should be a GCS prefix.
- `sqlite` profile: registry, parquet and online DB under the publication base
  (default `data/rag/feast/`, git-ignored). Labelled `local-dev` in every artifact.
- Feast Online (Redis or sqlite) is read by `chunk_id` only. Similarity is exact
  cosine in process (`FeastOnlineExactRetriever`). No Feast vector search is
  used or claimed.

### Dependencies (uv)

`infra/docker/rag-ingestion/pyproject.toml` + `uv.lock` is the task runtime
(Python 3.10, uv 0.13.5, 161 packages). It pins feast[gcp,redis] 0.66.0,
onnxruntime 1.23.2, tokenizers 0.23.3, huggingface-hub 2.4.0,
acryl-datahub[datahub-rest] 1.7.0.5, pandas 2.3.3, pyarrow 25.0.1 and
numpy 2.2.6, plus pytest 9.2.0 in the dev group. The Airflow scheduler stays
in its own environment and only orchestrates. Every task runs
`infra/docker/rag-ingestion/.venv/bin/python` (`EWALLET_RAG_PYTHON`), built with
`uv sync --locked --no-dev`, and a missing interpreter fails the task with
that command. CI: `Jenkinsfile.rag`.

### Running it for real

See `docs/llm_agent/rag_runbook.md` (post-merge reproduction, canonical Airflow run, evidence).

Clean-checkout check without the shared Airflow, which runs real Airflow `dags test`
in an isolated home with the sqlite profile and DataHub plan-only:

```bash
uv lock --check --project infra/docker/rag-ingestion
uv sync --locked --project infra/docker/rag-ingestion
uv run --locked --project infra/docker/rag-ingestion python -m pytest -q \
  tests/llm_agent/test_rag_airflow_ingestion.py tests/llm_agent/test_rag_online_retriever.py tests/llm_agent/test_phase_c.py
bash scripts/validation/validate_rag_dag_import.sh <airflow-env>/bin/python /tmp/rag-dag-parse.json
uv sync --locked --no-dev --project infra/docker/rag-ingestion
scripts/validation/run_rag_dag_isolated.sh <airflow-env>/bin/airflow <run-label>
```

Pass criteria: lock check passes, all tests pass, the parse report has no import errors,
6/6 tasks succeed, and `docs/evidence/llm_agent/rag_<run-label>/validation.json` shows
`readback_status=MATCH` and `recall_at_k=1.0`. Cleanup: delete that evidence directory
(the Feast files live in the temporary home and are already gone).
Rollback of a canonical publication: the rows are keyed by `chunk_id` under one
`feature_index_revision`. Re-materialize the previous revision, or remove the
`fraud_policy_chunks` view with `feast` against the same registry.

### Remaining gaps

- Canonical runtime: Feast Online Redis config (`online_repo`) is on the unmerged
  MLOps branch. Shared Airflow, Redis and DataHub runs have not happened, and no
  DataHub GMS is running locally.
- feature-rag-mcp image: `infra/docker/llm-agent-tooling` (G1's image) lacks the ONNX
  embedding packages. Adding them is a coordinated lock change after G1.
- Rubric R16: `chunk_lookup` (added 2026-11-01, see below) exists in feature-rag-mcp but is
  granted to nobody yet. Enabling it is a G1-owned Helm/gateway change.

### chunk_lookup (rubric edai-2 row 16)

`ai_platform/llm_agent/tools/chunk_lookup.py` serves `POST /v1/tools/chunk_lookup` and the MCP
tool `chunk_lookup` on feature-rag-mcp, a FastAPI service with pydantic validation and
`/health/live` and `/health/ready`. Input is `{"chunk_id": "<document_id>:<NNNN>"}`.
- The id must exist in `TrustedChunkIndex`. An unknown id returns `unknown_chunk` (422)
  without a backend call.
- The row is read from Feast Online by key and checked by `verify_chunk_row`, the same check
  knowledge_search uses: a tampered row gives `rag_index_provenance_mismatch` (502) and an
  unmaterialised one gives `rag_index_not_materialized` (503).
- The result carries the chunk text, document, ordinal and embedding dimension, plus one
  manifest-bound citation. Its score is 1.0 because this is an exact key lookup, not a
  similarity score.
- Default deny. The caller must be authenticated and its subject must hold the `chunk_lookup`
  grant (`EWALLET_MCP_TOOL_GRANTS`). The policy-only MCP gateway must also allow the tool name.
  To enable it, add `chunk_lookup` to `llmAgents.mcpServers.feature-rag-mcp.tools` and to
  `specialists.feature-rag-specialist.tools.feature-rag-mcp` in
  `infra/helm/llm-agent-platform/values.yaml`, then re-render the gateway policy. Both files
  are G1-owned, so this waits for coordination.

### Dependency scan

`Jenkinsfile.rag` runs the repository's pinned Trivy 0.75.0 on the committed lock
(vuln + secret, CRITICAL/HIGH, fixable). The one finding is CVE-2026-112940 in filelock
4.1.0. Its fix (4.2.1) requires Python >= 3.11, so it is a reviewed exception that expires
2027-01-31 (`infra/docker/rag-ingestion/trivyignore`). The affected `ReadWriteLock` is not
imported by any package in the lock.
