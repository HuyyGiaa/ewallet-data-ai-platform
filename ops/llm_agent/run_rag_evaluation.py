"""Run create-only RAG retrieval evaluation against an embedding endpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_platform.llm_agent.rag.evaluation import run_evaluation
from ai_platform.llm_agent.rag.pipeline import load_manifest
from ops.llm_agent.rag_pipeline import OpenAICompatibleEmbeddingClient, _read_secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--embedding-endpoint", required=True)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    client = OpenAICompatibleEmbeddingClient(
        endpoint=args.embedding_endpoint,
        model=manifest.embedding_model_id,
        api_key=_read_secret(args.api_key_file),
        timeout_seconds=args.timeout_seconds,
    )
    payload = run_evaluation(
        manifest_path=args.manifest,
        evaluation_path=args.evaluation,
        embed=client,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print(json.dumps(payload["metrics"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
