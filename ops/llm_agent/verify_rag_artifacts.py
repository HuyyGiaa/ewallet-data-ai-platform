"""Fail-closed verification for create-only RAG build/evaluation artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(build_path: Path, evaluation_path: Path) -> dict[str, object]:
    build = json.loads(build_path.read_text(encoding="utf-8"))
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    if build.get("runtime_verified") is not False or evaluation.get("runtime_verified") is not False:
        raise ValueError("source artifacts must not claim runtime verification")
    for key in ("corpus_revision", "embedding_model_revision", "feature_index_revision"):
        if not build.get(key) or build[key] != evaluation.get(key):
            raise ValueError(f"artifact identity mismatch: {key}")
    rows = build.get("feast_publication_rows")
    if not isinstance(rows, list) or len(rows) != build.get("chunk_count"):
        raise ValueError("Feast publication row count mismatch")
    required = {
        "document_id", "chunk_id", "source_uri", "source_checksum",
        "corpus_revision", "embedding_model_revision", "feature_index_revision",
        "event_timestamp", "publication_timestamp", "embedding",
    }
    if any(not required <= row.keys() for row in rows):
        raise ValueError("Feast publication row is missing provenance")
    plan = build.get("datahub_lineage_plan", {})
    if plan.get("runtime_verified") is not False or "upstreamLineage" not in plan.get("required_readback", []):
        raise ValueError("DataHub plan must require runtime lineage readback")
    metrics = evaluation.get("metrics", {})
    for key in ("recall_at_k", "mean_reciprocal_rank", "empty_retrieval_rate"):
        if key not in metrics:
            raise ValueError(f"missing evaluation metric: {key}")
    return {
        "status": "SOURCE_ONLY_PASS",
        "corpus_revision": build["corpus_revision"],
        "chunk_count": build["chunk_count"],
        "runtime_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-artifact", type=Path, required=True)
    parser.add_argument("--evaluation-artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(verify(args.build_artifact, args.evaluation_artifact), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
