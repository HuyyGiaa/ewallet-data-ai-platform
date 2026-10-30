"""Retrieval evaluation with source-preserving citations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .pipeline import (
    chunk_documents,
    citations,
    embed_chunks,
    evaluation_metrics,
    ingest_documents,
    retrieve_exact,
)


def run_evaluation(
    *, manifest_path: Path, evaluation_path: Path,
    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]],
) -> dict[str, object]:
    manifest, documents = ingest_documents(manifest_path)
    definition = json.loads(evaluation_path.read_text(encoding="utf-8"))
    queries = definition.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("evaluation queries must be a non-empty list")
    top_k = int(definition["top_k"])
    threshold = float(definition["score_threshold"])
    chunks = chunk_documents(
        documents,
        size_words=manifest.chunk_size_words,
        overlap_words=manifest.chunk_overlap_words,
    )
    corpus_vectors = embed([chunk.text for chunk in chunks])
    embedded = embed_chunks(
        chunks,
        embed=lambda _: corpus_vectors,
        dimensions=manifest.embedding_dimensions,
        model_revision=manifest.embedding_model_revision,
        index_revision=manifest.feature_index_revision,
        published_at=datetime.now(timezone.utc),
    )
    query_vectors = embed([str(item["text"]) for item in queries])
    if len(query_vectors) != len(queries):
        raise ValueError("query embedding response cardinality mismatch")
    result_sets = []
    expected_sets = []
    result_payloads = []
    for query, vector in zip(queries, query_vectors, strict=True):
        expected = {str(value) for value in query.get("expected_document_ids", [])}
        if not expected:
            raise ValueError("evaluation query is missing expected_document_ids")
        hits = retrieve_exact(vector, embedded, top_k=top_k, threshold=threshold)
        result_sets.append(hits)
        expected_sets.append(expected)
        result_payloads.append({
            "query_id": str(query["query_id"]),
            "retrieved_count": len(hits),
            "citations": [citation.__dict__ for citation in citations(hits)],
        })
    return {
        "evaluation_revision": str(definition["evaluation_revision"]),
        "corpus_revision": manifest.corpus_revision,
        "embedding_model_revision": manifest.embedding_model_revision,
        "feature_index_revision": manifest.feature_index_revision,
        "metrics": evaluation_metrics(result_sets, expected_sets),
        "results": result_payloads,
        "runtime_verified": False,
    }
