# Phase C — Versioned fraud-policy RAG

Status: source-only implementation. No Airflow DAG, embedding endpoint, Feast
registry/materialization, vector backend, retrieval workload or DataHub
publication has run.

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
