"""Call knowledge_search / chunk_lookup directly against the published Feast store.

Operator smoke for the RAG runtime: the same tool classes feature-rag-mcp
serves, wired to the Feast target the DAG published to (``--online-store``,
same configuration as ``ops.llm_agent.rag_ingest``) and the in-process ONNX
query embedder. It bypasses the MCP gateway and its authentication, so its
output is tool-level evidence, never MCP or Agent evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ops.llm_agent import rag_ingest

MANIFEST = "data/llm_agent/rag/fraud-policy-v1/manifest.json"


def _tools(online_store: str, manifest_path: Path) -> tuple[Any, Any]:
    from feast import FeatureStore

    from ai_platform.llm_agent.rag.embedding import LocalQueryEmbedder
    from ai_platform.llm_agent.rag.feast_publication import repo_config
    from ai_platform.llm_agent.rag.online_retriever import FeastChunkRowSource, FeastOnlineExactRetriever
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
    from ai_platform.llm_agent.tools.chunk_lookup import ChunkLookupTool
    from ai_platform.llm_agent.tools.knowledge_search import KnowledgeSearchTool

    index = TrustedChunkIndex.from_manifest(manifest_path)
    target = rag_ingest._target(argparse.Namespace(online_store=online_store), index.manifest)
    rows = FeastChunkRowSource(FeatureStore(config=repo_config(target)), timeout_seconds=30.0)
    retriever = FeastOnlineExactRetriever(index, rows, LocalQueryEmbedder(rag_ingest._embedder(index.manifest)))
    return KnowledgeSearchTool(retriever), ChunkLookupTool(index, rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    from ai_platform.llm_agent.contracts import RequestContext
    from ai_platform.llm_agent.tools.chunk_lookup import ChunkLookupInput
    from ai_platform.llm_agent.tools.knowledge_search import KnowledgeSearchInput

    rag_ingest.load_env_file()
    search, lookup = _tools(args.online_store, rag_ingest.REPOSITORY_ROOT / args.manifest)
    context = RequestContext("rag-query-cli", "00-" + "0" * 31 + "1-" + "0" * 15 + "1-01", "operator", 30000, 1)

    async def call() -> dict[str, Any]:
        if args.chunk_id:
            outcome = await lookup.run(ChunkLookupInput(chunk_id=args.chunk_id), context)
        else:
            outcome = await search.run(KnowledgeSearchInput(query=args.query, top_k=args.top_k,
                                                            min_score=args.min_score), context)
        return {"tool": lookup.name if args.chunk_id else search.name, "result": dict(outcome.result),
                "citations": [citation.__dict__ for citation in outcome.citations],
                "version_overrides": dict(outcome.version_overrides),
                "path": "direct tool call (no MCP gateway, no authentication)"}

    return asyncio.run(call())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--query")
    target.add_argument("--chunk-id")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--online-store", choices=("redis", "sqlite"), default="redis")
    parser.add_argument("--manifest", default=MANIFEST)
    args = parser.parse_args(argv)
    print(json.dumps(run(args), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
