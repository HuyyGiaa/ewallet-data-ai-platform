"""knowledge_search: fraud-policy retrieval returning manifest-bound citations.

Every hit is converted through ``to_agent_citations`` against the trusted
chunk index, so a retriever cannot introduce a fabricated chunk, quote or
revision. The production retriever is explicitly unavailable until an
approved Feast vector-search backend exists (Redis is publication-only).
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.rag.contracts import Citation as RagCitation
from ai_platform.llm_agent.rag.feast_contract import PINNED_FEAST_VERSION, assess_vector_compatibility
from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex, to_agent_citations
from ai_platform.llm_agent.tools.runtime import ToolContractError, ToolOutcome, ToolUnavailableError

MAX_TOP_K = 5


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]
    top_k: Annotated[int, Field(ge=1, le=MAX_TOP_K, strict=True)] = 3
    min_score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = 0.0

    @field_validator("query")
    @classmethod
    def _printable(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\t\n" for char in value) or "\x7f" in value:
            raise ValueError("query must not contain control characters")
        return value


class KnowledgeRetriever(Protocol):
    backend_id: str
    index: TrustedChunkIndex

    async def search(self, query: str, *, top_k: int, min_score: float) -> Sequence[RagCitation]: ...

    async def ready(self) -> None: ...


class UnavailableKnowledgeRetriever:
    """Production placeholder: reports the vector-search blocker, never results."""

    backend_id = "feast-online-vector-search"

    def __init__(self, index: TrustedChunkIndex, *, online_store: str = "redis") -> None:
        self.index = index
        compatibility = assess_vector_compatibility(feast_version=PINNED_FEAST_VERSION, online_store=online_store)
        if compatibility.vector_search_supported:
            raise ValueError("a supported vector store needs a real retriever, not this placeholder")
        self._reason = f"{compatibility.status}: {compatibility.blocker}"

    async def search(self, query: str, *, top_k: int, min_score: float) -> Sequence[RagCitation]:
        raise ToolUnavailableError("vector_search_unavailable", self._reason)

    async def ready(self) -> None:
        raise ToolUnavailableError("vector_search_unavailable", self._reason)


class KnowledgeSearchTool:
    name = "knowledge_search"
    description = (
        "Search the versioned fraud-policy corpus and return citations bound to the "
        "published manifest (document checksum, chunk, corpus/embedding/index revisions)."
    )
    input_model = KnowledgeSearchInput

    def __init__(self, retriever: KnowledgeRetriever) -> None:
        self._retriever = retriever

    async def run(self, arguments: KnowledgeSearchInput, context: RequestContext) -> ToolOutcome:
        hits = tuple(await self._retriever.search(
            arguments.query, top_k=arguments.top_k, min_score=arguments.min_score))
        if len(hits) > arguments.top_k:
            raise ToolContractError("retrieval_bound_exceeded", "Retriever returned more results than requested")
        if len({hit.chunk_id for hit in hits}) != len(hits):
            raise ToolContractError("duplicate_citation", "Retriever returned a chunk more than once")
        if any(not isinstance(hit.score, (int, float)) or hit.score < arguments.min_score for hit in hits):
            raise ToolContractError("retrieval_threshold_violation", "Retriever returned a result below min_score")
        if any(left.score < right.score for left, right in zip(hits, hits[1:])):
            raise ToolContractError("retrieval_order_violation", "Retriever results are not ranked by score")
        index = self._retriever.index
        try:
            citations = to_agent_citations(hits, index=index)
        except ValueError as exc:
            raise ToolContractError("citation_integrity_error", "Retriever returned an untrusted citation") from exc
        return ToolOutcome(
            result={
                # The query is not echoed: it may contain customer identifiers.
                "query_sha256": hashlib.sha256(arguments.query.encode("utf-8")).hexdigest(),
                "results": [
                    {"rank": rank, "chunk_id": hit.chunk_id, "document_id": hit.document_id,
                     "score": float(hit.score), "quote": hit.quote}
                    for rank, hit in enumerate(hits, start=1)
                ],
                "retrieval_backend": self._retriever.backend_id,
                "trust_anchor": index.evidence,
            },
            citations=citations,
            version_overrides={
                "corpus_revision": index.manifest.corpus_revision,
                "feature_revision": index.manifest.feature_index_revision,
            },
        )

    async def ready(self) -> None:
        await self._retriever.ready()
