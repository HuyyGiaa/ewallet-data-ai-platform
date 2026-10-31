"""Build a create-only Feast publication artifact through a real embedding API."""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ai_platform.llm_agent.rag.feast_contract import (
    PINNED_FEAST_VERSION,
    assess_vector_compatibility,
    datahub_lineage_plan,
)
from ai_platform.llm_agent.rag.pipeline import (
    chunk_documents,
    embed_chunks,
    feast_publication_rows,
    ingest_documents,
)


class OpenAICompatibleEmbeddingClient:
    def __init__(self, *, endpoint: str, model: str, api_key: str | None, timeout_seconds: int) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("embedding endpoint must be HTTP(S)")
        if timeout_seconds < 1 or timeout_seconds > 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        self.endpoint = endpoint.rstrip("/") + "/v1/embeddings"
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def __call__(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        body = json.dumps({"model": self.model, "input": list(texts), "encoding_format": "float"}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.load(response)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("embedding response is missing data")
        ordered = sorted(data, key=lambda item: int(item["index"]))
        if [int(item["index"]) for item in ordered] != list(range(len(texts))):
            raise ValueError("embedding response indexes are not contiguous")
        return [item["embedding"] for item in ordered]


def _read_secret(path: Path | None) -> str | None:
    if path is None:
        return None
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("API key file is empty")
    return value


def build_artifact(args: argparse.Namespace) -> dict[str, object]:
    manifest, documents = ingest_documents(args.manifest)
    chunks = chunk_documents(
        documents,
        size_words=manifest.chunk_size_words,
        overlap_words=manifest.chunk_overlap_words,
    )
    client = OpenAICompatibleEmbeddingClient(
        endpoint=args.embedding_endpoint,
        model=manifest.embedding_model_id,
        api_key=_read_secret(args.api_key_file),
        timeout_seconds=args.timeout_seconds,
    )
    published_at = datetime.now(timezone.utc)
    embedded = embed_chunks(
        chunks,
        embed=client,
        dimensions=manifest.embedding_dimensions,
        model_revision=manifest.embedding_model_revision,
        index_revision=manifest.feature_index_revision,
        published_at=published_at,
    )
    compatibility = assess_vector_compatibility(
        feast_version=PINNED_FEAST_VERSION,
        online_store=args.online_store,
    )
    return {
        "schema_version": 1,
        "generated_at": published_at.isoformat(),
        "corpus_revision": manifest.corpus_revision,
        "embedding_model_id": manifest.embedding_model_id,
        "embedding_model_revision": manifest.embedding_model_revision,
        "feature_index_revision": manifest.feature_index_revision,
        "document_count": len(documents),
        "chunk_count": len(embedded),
        "feast_compatibility": compatibility.__dict__,
        "feast_publication_rows": feast_publication_rows(embedded),
        "datahub_lineage_plan": datahub_lineage_plan(
            corpus_revision=manifest.corpus_revision,
            feature_index_revision=manifest.feature_index_revision,
        ),
        "runtime_verified": False,
    }


def validate_manifest(path: Path) -> dict[str, object]:
    manifest, documents = ingest_documents(path)
    chunks = chunk_documents(
        documents,
        size_words=manifest.chunk_size_words,
        overlap_words=manifest.chunk_overlap_words,
    )
    return {
        "status": "PASS",
        "corpus_revision": manifest.corpus_revision,
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "embedding_model_revision": manifest.embedding_model_revision,
        "runtime_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--embedding-endpoint")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--online-store", default="redis")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.validate_only:
        print(json.dumps(validate_manifest(args.manifest), sort_keys=True))
        return 0
    if args.embedding_endpoint is None or args.output is None:
        parser.error("build mode requires --embedding-endpoint and --output")
    payload = build_artifact(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print(json.dumps({key: payload[key] for key in ("corpus_revision", "document_count", "chunk_count", "runtime_verified")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
