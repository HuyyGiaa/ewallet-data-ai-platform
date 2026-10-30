"""Fail-closed transport-neutral contracts for the fraud-policy corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Mapping, Sequence


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PolicyDocument:
    document_id: str
    source_uri: str
    source_checksum: str
    corpus_revision: str
    event_timestamp: datetime
    text: str

    def __post_init__(self) -> None:
        for name in ("document_id", "source_uri", "source_checksum", "corpus_revision"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "event_timestamp", _utc(self.event_timestamp, "event_timestamp"))
        if len(self.source_checksum) != 64 or any(c not in "0123456789abcdef" for c in self.source_checksum):
            raise ValueError("source_checksum must be a lowercase SHA-256 digest")
        if not self.text.strip():
            raise ValueError("document text is empty")


@dataclass(frozen=True)
class PolicyChunk:
    document_id: str
    chunk_id: str
    source_uri: str
    source_checksum: str
    corpus_revision: str
    ordinal: int
    text: str
    event_timestamp: datetime

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        if not self.text.strip():
            raise ValueError("chunk text is empty")
        object.__setattr__(self, "event_timestamp", _utc(self.event_timestamp, "event_timestamp"))


@dataclass(frozen=True)
class EmbeddedChunk:
    chunk: PolicyChunk
    embedding: tuple[float, ...]
    embedding_model_revision: str
    feature_index_revision: str
    publication_timestamp: datetime

    def __post_init__(self) -> None:
        if not self.embedding or any(not math.isfinite(value) for value in self.embedding):
            raise ValueError("embedding must contain finite values")
        if sum(value * value for value in self.embedding) == 0:
            raise ValueError("embedding must have non-zero norm")
        object.__setattr__(self, "embedding_model_revision", _required(self.embedding_model_revision, "embedding_model_revision"))
        object.__setattr__(self, "feature_index_revision", _required(self.feature_index_revision, "feature_index_revision"))
        object.__setattr__(self, "publication_timestamp", _utc(self.publication_timestamp, "publication_timestamp"))


@dataclass(frozen=True)
class RetrievalHit:
    embedded_chunk: EmbeddedChunk
    score: float
    rank: int

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("rank must be at least one")
        if self.score < -1.0 or self.score > 1.0:
            raise ValueError("cosine score must be between -1 and 1")


@dataclass(frozen=True)
class Citation:
    document_id: str
    chunk_id: str
    source_uri: str
    source_checksum: str
    corpus_revision: str
    feature_index_revision: str
    quote: str
    score: float


@dataclass(frozen=True)
class CorpusManifest:
    corpus_id: str
    corpus_revision: str
    embedding_model_id: str
    embedding_model_revision: str
    feature_index_revision: str
    embedding_dimensions: int
    chunk_size_words: int
    chunk_overlap_words: int
    documents: tuple[Mapping[str, str], ...]

    def __post_init__(self) -> None:
        for name in (
            "corpus_id", "corpus_revision", "embedding_model_id",
            "embedding_model_revision", "feature_index_revision",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be positive")
        if self.chunk_size_words < 16:
            raise ValueError("chunk_size_words must be at least 16")
        if not 0 <= self.chunk_overlap_words < self.chunk_size_words:
            raise ValueError("chunk overlap must be non-negative and smaller than chunk size")
        if not self.documents:
            raise ValueError("manifest must contain documents")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CorpusManifest":
        documents = value.get("documents")
        if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
            raise ValueError("documents must be a sequence")
        return cls(
            corpus_id=str(value["corpus_id"]),
            corpus_revision=str(value["corpus_revision"]),
            embedding_model_id=str(value["embedding_model_id"]),
            embedding_model_revision=str(value["embedding_model_revision"]),
            feature_index_revision=str(value["feature_index_revision"]),
            embedding_dimensions=int(value["embedding_dimensions"]),
            chunk_size_words=int(value["chunk_size_words"]),
            chunk_overlap_words=int(value["chunk_overlap_words"]),
            documents=tuple(dict(item) for item in documents),
        )
