"""Pure pipeline stages shared by Airflow, tests and the runtime runner."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

from ai_platform.llm_agent.contracts import Citation as AgentCitation

from .contracts import Citation, CorpusManifest, EmbeddedChunk, PolicyChunk, PolicyDocument, RetrievalHit

SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


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
        embedding_model_revision=hit.embedded_chunk.embedding_model_revision,
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


@dataclass(frozen=True)
class TrustedChunkIndex:
    """Exact chunk identities that a citation is allowed to reference.

    The canonical set is rebuilt from checksum-verified source documents with
    the manifest chunking parameters. A publication artifact is accepted only
    when its rows equal that set exactly. This binds citations to the source
    corpus; it is not a readback of a Feast-published index, which remains a
    runtime gate (``evidence`` records which artifact was checked).
    """

    manifest: CorpusManifest
    chunks: Mapping[str, PolicyChunk]
    evidence: str

    @classmethod
    def from_manifest(cls, manifest_path: Path) -> "TrustedChunkIndex":
        manifest, documents = ingest_documents(manifest_path)
        chunks = chunk_documents(
            documents, size_words=manifest.chunk_size_words, overlap_words=manifest.chunk_overlap_words,
        )
        return cls(manifest, MappingProxyType({chunk.chunk_id: chunk for chunk in chunks}), "source_corpus")

    @classmethod
    def from_publication_rows(
        cls, manifest_path: Path, rows: Sequence[Mapping[str, object]],
    ) -> "TrustedChunkIndex":
        canonical = cls.from_manifest(manifest_path)
        manifest = canonical.manifest
        seen: set[str] = set()
        for row in rows:
            chunk_id = str(row.get("chunk_id", ""))
            chunk = canonical.chunks.get(chunk_id)
            if chunk is None or chunk_id in seen:
                raise ValueError(f"publication row is unknown or duplicated: {chunk_id!r}")
            seen.add(chunk_id)
            expected = {
                "document_id": chunk.document_id,
                "chunk_text": chunk.text,
                "source_uri": chunk.source_uri,
                "source_checksum": chunk.source_checksum,
                "corpus_revision": manifest.corpus_revision,
                "embedding_model_revision": manifest.embedding_model_revision,
                "feature_index_revision": manifest.feature_index_revision,
            }
            mismatched = sorted(name for name, value in expected.items() if row.get(name) != value)
            if mismatched:
                raise ValueError(f"publication row {chunk_id!r} differs from the source corpus: {mismatched}")
        if seen != set(canonical.chunks):
            raise ValueError("publication rows do not cover the canonical chunk set")
        return cls(manifest, canonical.chunks, "publication_artifact")


def to_agent_citation(citation: Citation, *, index: TrustedChunkIndex) -> AgentCitation:
    """Convert an internal cosine-search citation to the shared Agent/MCP contract.

    RAG stores the document SHA-256 as 64 bare lowercase hex characters; the
    shared contract uses ``sha256:<hex>``. Every citation must reference an
    exact chunk of the trusted index and the manifest revision set, and its
    quote must be that chunk's leading words, so a citation can be neither
    fabricated nor relabelled. Negative or non-finite cosine scores are
    rejected rather than clipped into a false [0, 1] confidence.
    """
    manifest = index.manifest
    documents = {str(item["document_id"]): item for item in manifest.documents}
    source = documents.get(citation.document_id)
    if source is None:
        raise ValueError(f"citation document is not in the manifest: {citation.document_id!r}")
    if not SHA256_HEX_RE.fullmatch(citation.source_checksum):
        raise ValueError("RAG source_checksum must be 64 lowercase hex characters")
    if citation.source_checksum != str(source["sha256"]) or citation.source_uri != str(source["source_uri"]):
        raise ValueError(f"citation source does not match the manifest for {citation.document_id!r}")
    chunk = index.chunks.get(citation.chunk_id)
    if chunk is None or chunk.document_id != citation.document_id:
        raise ValueError(f"chunk_id is not a trusted chunk of the cited document: {citation.chunk_id!r}")
    quote = citation.quote.split()
    if not quote or chunk.text.split()[: len(quote)] != quote:
        raise ValueError("citation quote is not taken from the cited chunk")
    for name, expected in (
        ("corpus_revision", manifest.corpus_revision),
        ("embedding_model_revision", manifest.embedding_model_revision),
        ("feature_index_revision", manifest.feature_index_revision),
    ):
        if getattr(citation, name) != expected:
            raise ValueError(f"citation {name} does not match the published manifest")
    score = citation.score
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise ValueError("citation score must be a finite number")
    return AgentCitation(
        document_id=citation.document_id,
        chunk_id=citation.chunk_id,
        source_uri=citation.source_uri,
        source_checksum=f"sha256:{citation.source_checksum}",
        corpus_revision=citation.corpus_revision,
        embedding_revision=citation.embedding_model_revision,
        feature_revision=citation.feature_index_revision,
        score=float(score),
    )


def to_agent_citations(rag_citations: Iterable[Citation], *, index: TrustedChunkIndex) -> tuple[AgentCitation, ...]:
    """Validate and convert every source citation before an MCP response."""
    return tuple(to_agent_citation(citation, index=index) for citation in rag_citations)
