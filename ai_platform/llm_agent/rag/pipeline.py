"""Pure pipeline stages shared by Airflow, tests and the runtime runner."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .contracts import Citation, CorpusManifest, EmbeddedChunk, PolicyChunk, PolicyDocument, RetrievalHit


def load_manifest(path: Path) -> CorpusManifest:
    return CorpusManifest.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def ingest_documents(manifest_path: Path) -> tuple[CorpusManifest, tuple[PolicyDocument, ...]]:
    manifest = load_manifest(manifest_path)
    root = manifest_path.parent
    documents: list[PolicyDocument] = []
    seen_ids: set[str] = set()
    for entry in manifest.documents:
        document_id = str(entry["document_id"])
        if document_id in seen_ids:
            raise ValueError(f"duplicate document_id: {document_id}")
        seen_ids.add(document_id)
        relative_path = Path(str(entry["path"]))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe document path: {relative_path}")
        path = root / relative_path
        payload = path.read_bytes()
        actual_checksum = hashlib.sha256(payload).hexdigest()
        expected_checksum = str(entry["sha256"])
        if actual_checksum != expected_checksum:
            raise ValueError(f"checksum mismatch for {document_id}")
        documents.append(PolicyDocument(
            document_id=document_id,
            source_uri=str(entry["source_uri"]),
            source_checksum=actual_checksum,
            corpus_revision=manifest.corpus_revision,
            event_timestamp=datetime.fromisoformat(str(entry["event_timestamp"]).replace("Z", "+00:00")),
            text=payload.decode("utf-8"),
        ))
    return manifest, tuple(documents)


def chunk_documents(
    documents: Iterable[PolicyDocument], *, size_words: int, overlap_words: int
) -> tuple[PolicyChunk, ...]:
    if size_words < 16 or not 0 <= overlap_words < size_words:
        raise ValueError("invalid chunk parameters")
    chunks: list[PolicyChunk] = []
    step = size_words - overlap_words
    for document in documents:
        words = document.text.split()
        for ordinal, start in enumerate(range(0, len(words), step)):
            text = " ".join(words[start : start + size_words]).strip()
            if not text:
                continue
            chunks.append(PolicyChunk(
                document_id=document.document_id,
                chunk_id=f"{document.document_id}:{ordinal:04d}",
                source_uri=document.source_uri,
                source_checksum=document.source_checksum,
                corpus_revision=document.corpus_revision,
                ordinal=ordinal,
                text=text,
                event_timestamp=document.event_timestamp,
            ))
            if start + size_words >= len(words):
                break
    return tuple(chunks)


def embed_chunks(
    chunks: Sequence[PolicyChunk], *, embed: Callable[[Sequence[str]], Sequence[Sequence[float]]],
    dimensions: int, model_revision: str, index_revision: str, published_at: datetime,
) -> tuple[EmbeddedChunk, ...]:
    vectors = embed([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("embedding response cardinality mismatch")
    result = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        if len(vector) != dimensions:
            raise ValueError(f"embedding dimension mismatch for {chunk.chunk_id}")
        result.append(EmbeddedChunk(
            chunk=chunk,
            embedding=tuple(float(value) for value in vector),
            embedding_model_revision=model_revision,
            feature_index_revision=index_revision,
            publication_timestamp=published_at,
        ))
    return tuple(result)


def feast_publication_rows(chunks: Iterable[EmbeddedChunk]) -> tuple[dict[str, object], ...]:
    rows = []
    for item in chunks:
        chunk = item.chunk
        rows.append({
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "chunk_text": chunk.text,
            "source_uri": chunk.source_uri,
            "source_checksum": chunk.source_checksum,
            "corpus_revision": chunk.corpus_revision,
            "embedding_model_revision": item.embedding_model_revision,
            "feature_index_revision": item.feature_index_revision,
            "embedding": list(item.embedding),
            "event_timestamp": chunk.event_timestamp.isoformat(),
            "publication_timestamp": item.publication_timestamp.isoformat(),
        })
    return tuple(rows)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("query and document embeddings must have equal non-zero dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("cannot compare a zero-norm embedding")
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def retrieve_exact(
    query_embedding: Sequence[float], chunks: Iterable[EmbeddedChunk], *, top_k: int, threshold: float,
) -> tuple[RetrievalHit, ...]:
    if top_k < 1 or not -1.0 <= threshold <= 1.0:
        raise ValueError("invalid retrieval bounds")
    scored = sorted(
        ((item, _cosine(query_embedding, item.embedding)) for item in chunks),
        key=lambda pair: (-pair[1], pair[0].chunk.chunk_id),
    )
    return tuple(
        RetrievalHit(embedded_chunk=item, score=score, rank=rank)
        for rank, (item, score) in enumerate(
            ((item, score) for item, score in scored if score >= threshold), start=1
        )
        if rank <= top_k
    )


def citations(hits: Iterable[RetrievalHit], *, quote_words: int = 40) -> tuple[Citation, ...]:
    if quote_words < 1:
        raise ValueError("quote_words must be positive")
    return tuple(Citation(
        document_id=hit.embedded_chunk.chunk.document_id,
        chunk_id=hit.embedded_chunk.chunk.chunk_id,
        source_uri=hit.embedded_chunk.chunk.source_uri,
        source_checksum=hit.embedded_chunk.chunk.source_checksum,
        corpus_revision=hit.embedded_chunk.chunk.corpus_revision,
        feature_index_revision=hit.embedded_chunk.feature_index_revision,
        quote=" ".join(hit.embedded_chunk.chunk.text.split()[:quote_words]),
        score=hit.score,
    ) for hit in hits)


def evaluation_metrics(results: Sequence[Sequence[RetrievalHit]], expected_document_ids: Sequence[set[str]]) -> dict[str, float]:
    if not results or len(results) != len(expected_document_ids):
        raise ValueError("evaluation requires aligned non-empty results and labels")
    recalls = []
    reciprocal_ranks = []
    for hits, expected in zip(results, expected_document_ids, strict=True):
        if not expected:
            raise ValueError("each evaluation query requires expected documents")
        retrieved = {hit.embedded_chunk.chunk.document_id for hit in hits}
        recalls.append(len(retrieved & expected) / len(expected))
        reciprocal_ranks.append(next((1.0 / hit.rank for hit in hits if hit.embedded_chunk.chunk.document_id in expected), 0.0))
    return {
        "query_count": float(len(results)),
        "recall_at_k": sum(recalls) / len(recalls),
        "mean_reciprocal_rank": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "empty_retrieval_rate": sum(not hits for hits in results) / len(results),
    }
