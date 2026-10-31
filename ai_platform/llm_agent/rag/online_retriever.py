"""Exact fraud-policy retrieval over chunks served by the Feast online store.

Feast 0.66 with the Redis online store stores ``Array(Float32)`` embeddings but
cannot search them (``feast_contract``: PUBLICATION_ONLY). This retriever keeps
the two concerns apart:

- storage: chunk text, provenance and embedding are read from Feast Online by
  ``chunk_id`` (a key lookup Redis supports), for exactly the canonical chunk
  set rebuilt from the checksum-verified corpus (``TrustedChunkIndex``);
- search: cosine similarity is computed in process over those rows
  (``retrieve_exact``), with the query embedded by the same pinned embedding
  model revision.

Every served row must match the canonical chunk, corpus, embedding model and
index revision, or the tool fails closed. Exact search is O(chunks) per query;
``max_chunks`` bounds it for the coursework corpus (2 documents). A larger
corpus needs an approved vector-search backend instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import time
from typing import Any, Mapping, Protocol, Sequence
import urllib.request

from ai_platform.llm_agent.rag.contracts import Citation, EmbeddedChunk
from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex, citations, retrieve_exact
from ai_platform.llm_agent.tools.runtime import ToolContractError, ToolUnavailableError

FEATURE_VIEW = "fraud_policy_chunks"
ROW_FIELDS = ("document_id", "chunk_text", "source_uri", "source_checksum", "corpus_revision",
              "embedding_model_revision", "feature_index_revision", "embedding")


class ChunkRowSource(Protocol):
    async def fetch(self, chunk_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]: ...


class QueryEmbedder(Protocol):
    model_revision: str
    dimensions: int

    async def embed(self, text: str) -> Sequence[float]: ...


class FeastChunkRowSource:
    """Batch ``get_online_features`` for chunk ids, off the event loop."""

    def __init__(self, store: Any, *, timeout_seconds: float) -> None:
        self._store = store
        self._timeout = timeout_seconds

    def _fetch(self, chunk_ids: Sequence[str]) -> list[dict[str, Any]]:
        response = self._store.get_online_features(
            features=[f"{FEATURE_VIEW}:{name}" for name in ROW_FIELDS],
            entity_rows=[{"chunk_id": chunk_id} for chunk_id in chunk_ids],
        ).to_dict()
        columns = {name: response.get(name) for name in ("chunk_id", *ROW_FIELDS)}
        if any(not isinstance(values, list) or len(values) != len(chunk_ids) for values in columns.values()):
            raise ToolContractError("rag_index_contract_error", "Online store returned a malformed chunk batch")
        return [{name: columns[name][i] for name in columns} for i in range(len(chunk_ids))]

    async def fetch(self, chunk_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._fetch, list(chunk_ids)), self._timeout)
        except ToolContractError:
            raise
        except asyncio.TimeoutError as exc:
            raise ToolUnavailableError("rag_index_timeout", "Feast online store timed out") from exc
        except Exception as exc:
            raise ToolUnavailableError("rag_index_unavailable", "Feast online store is unavailable") from exc


@dataclass(frozen=True)
class OpenAICompatibleQueryEmbedder:
    """Embeds one query with the pinned model through an OpenAI-compatible API."""

    endpoint: str
    model: str
    model_revision: str
    dimensions: int
    timeout_seconds: float = 10.0
    api_key: str | None = None

    def _embed(self, text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps({"model": self.model, "input": [text], "encoding_format": "float"}).encode()
        request = urllib.request.Request(self.endpoint.rstrip("/") + "/v1/embeddings", body, headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 (configured endpoint)
            payload = json.load(response)
        return payload["data"][0]["embedding"]

    async def embed(self, text: str) -> Sequence[float]:
        try:
            return await asyncio.to_thread(self._embed, text)
        except Exception as exc:
            raise ToolUnavailableError("embedding_unavailable", "Query embedding service is unavailable") from exc


class FeastOnlineExactRetriever:
    backend_id = "feast-online-redis+exact-cosine"

    def __init__(self, index: TrustedChunkIndex, rows: ChunkRowSource, embedder: QueryEmbedder, *,
                 max_chunks: int = 2000, cache_seconds: float = 300.0) -> None:
        manifest = index.manifest
        if embedder.model_revision != manifest.embedding_model_revision:
            raise ValueError("query embedder revision differs from the corpus embedding revision")
        if embedder.dimensions != manifest.embedding_dimensions:
            raise ValueError("query embedder dimensions differ from the corpus embedding dimensions")
        if len(index.chunks) > max_chunks:
            raise ValueError(f"{len(index.chunks)} chunks exceed the exact-search bound {max_chunks}")
        self.index = index
        self._rows = rows
        self._embedder = embedder
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, tuple[EmbeddedChunk, ...]] | None = None
        self._lock = asyncio.Lock()

    def _verify(self, rows: Sequence[Mapping[str, Any]]) -> tuple[EmbeddedChunk, ...]:
        manifest = self.index.manifest
        verified = []
        for row in rows:
            chunk = self.index.chunks.get(str(row.get("chunk_id")))
            if chunk is None:
                raise ToolContractError("rag_index_contract_error", "Online store returned an unknown chunk")
            if any(row.get(name) is None for name in ROW_FIELDS):
                # Not materialized (or expired): never answer from a partial index.
                raise ToolUnavailableError("rag_index_not_materialized",
                                           "The published RAG index is not materialized in the online store")
            expected = {
                "document_id": chunk.document_id, "chunk_text": chunk.text, "source_uri": chunk.source_uri,
                "source_checksum": chunk.source_checksum, "corpus_revision": manifest.corpus_revision,
                "embedding_model_revision": manifest.embedding_model_revision,
                "feature_index_revision": manifest.feature_index_revision,
            }
            if any(row[name] != value for name, value in expected.items()):
                raise ToolContractError("rag_index_provenance_mismatch",
                                        "Online chunk differs from the verified corpus or revisions")
            embedding = tuple(float(value) for value in row["embedding"])
            if len(embedding) != manifest.embedding_dimensions:
                raise ToolContractError("rag_index_contract_error", "Online chunk embedding has the wrong dimensions")
            try:
                verified.append(EmbeddedChunk(
                    chunk=chunk, embedding=embedding,
                    embedding_model_revision=manifest.embedding_model_revision,
                    feature_index_revision=manifest.feature_index_revision,
                    # The online store serves no creation time; the source
                    # chunk's event time is the provenance timestamp.
                    publication_timestamp=chunk.event_timestamp,
                ))
            except ValueError as exc:
                raise ToolContractError("rag_index_contract_error", "Online chunk embedding is invalid") from exc
        if {item.chunk.chunk_id for item in verified} != set(self.index.chunks) or len(verified) != len(self.index.chunks):
            raise ToolContractError("rag_index_contract_error", "Online store did not return the canonical chunk set")
        return tuple(verified)

    async def _load(self) -> tuple[EmbeddedChunk, ...]:
        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached[0] < self._cache_seconds:
                return self._cached[1]
            rows = await self._rows.fetch(sorted(self.index.chunks))
            chunks = self._verify(rows)
            self._cached = (now, chunks)
            return chunks

    async def ready(self) -> None:
        await self._load()

    async def search(self, query: str, *, top_k: int, min_score: float) -> Sequence[Citation]:
        chunks = await self._load()
        vector = tuple(float(value) for value in await self._embedder.embed(query))
        if len(vector) != self.index.manifest.embedding_dimensions or not all(map(math.isfinite, vector)):
            raise ToolContractError("embedding_contract_error", "Query embedding has the wrong shape")
        try:
            hits = retrieve_exact(vector, chunks, top_k=top_k, threshold=min_score)
        except ValueError as exc:
            raise ToolContractError("embedding_contract_error", "Query embedding cannot be compared") from exc
        return citations(hits)
