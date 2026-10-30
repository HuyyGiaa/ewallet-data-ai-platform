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
