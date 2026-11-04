"""chunk_lookup: one published fraud-policy chunk by its stable chunk_id.

Rubric edai-2 row 16 (Web API reading chunks from the Online Feature Store by
ID). The id must belong to the trusted chunk index rebuilt from the
checksum-verified manifest; the row is read from Feast Online by key and must
match that chunk and the manifest revisions (``verify_chunk_row``), so the
tool can neither invent nor relabel a chunk. Like every tool it is callable
only by subjects granted ``chunk_lookup`` (default deny).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.rag.contracts import Citation as RagCitation
from ai_platform.llm_agent.rag.online_retriever import ChunkRowSource, verify_chunk_row
from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex, to_agent_citation
from ai_platform.llm_agent.tools.runtime import ToolContractError, ToolOutcome, ToolRejectedError

CHUNK_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,127}:[0-9]{4}$"
QUOTE_WORDS = 40


class ChunkLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: Annotated[str, StringConstraints(strip_whitespace=True, pattern=CHUNK_ID_PATTERN)]


class ChunkLookupTool:
    name = "chunk_lookup"
    description = (
        "Return one published fraud-policy chunk by chunk_id from Feast Online, with its document, "
        "source checksum and corpus/embedding/index revisions."
    )
    input_model = ChunkLookupInput

    def __init__(self, index: TrustedChunkIndex, rows: ChunkRowSource) -> None:
        if any(not re.fullmatch(CHUNK_ID_PATTERN, chunk_id) for chunk_id in index.chunks):
            raise ValueError("the trusted index contains chunk ids outside the lookup pattern")
        self._index = index
        self._rows = rows

    async def run(self, arguments: ChunkLookupInput, context: RequestContext) -> ToolOutcome:
        if arguments.chunk_id not in self._index.chunks:
            raise ToolRejectedError("unknown_chunk", "No published chunk has this chunk_id")
        rows = await self._rows.fetch([arguments.chunk_id])
        if len(rows) != 1:
            raise ToolContractError("rag_index_contract_error", "Online store returned a malformed chunk batch")
        item = verify_chunk_row(self._index, rows[0])
        chunk = item.chunk
        quote = " ".join(chunk.text.split()[:QUOTE_WORDS])
        citation = to_agent_citation(RagCitation(
            document_id=chunk.document_id, chunk_id=chunk.chunk_id, source_uri=chunk.source_uri,
            source_checksum=chunk.source_checksum, corpus_revision=chunk.corpus_revision,
            embedding_model_revision=item.embedding_model_revision,
            feature_index_revision=item.feature_index_revision, quote=quote,
            # An exact key match, not a similarity score.
            score=1.0,
        ), index=self._index)
        manifest = self._index.manifest
        return ToolOutcome(
            result={
                "chunk_id": chunk.chunk_id, "document_id": chunk.document_id, "ordinal": chunk.ordinal,
                "chunk_text": chunk.text, "source_uri": chunk.source_uri,
                "embedding_dimensions": len(item.embedding), "retrieval_backend": "feast-online-redis+key-lookup",
            },
            citations=(citation,),
            version_overrides={
                "corpus_revision": manifest.corpus_revision,
                "feature_revision": manifest.feature_index_revision,
            },
        )

    async def ready(self) -> None:
        # Readiness = the first canonical chunk is materialized and matches the corpus.
        first = min(self._index.chunks)
        rows = await self._rows.fetch([first])
        verify_chunk_row(self._index, rows[0])
