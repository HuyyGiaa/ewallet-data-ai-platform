"""Fail-closed verdict for the row 33 RAG CD smoke Job.

The Job prints one ``RAG_SMOKE_SUMMARY <json>`` line (scripts/ci/rag_smoke.sh).
A PASS requires the deployed image's release SHA, the frozen chunking and
embedding identity of the production publication, an exact online readback and
the retrieval quality recorded in jenkins/cd/rag-pipeline.contract.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MARKER = "RAG_SMOKE_SUMMARY "


def _summary(log: str) -> dict[str, Any] | None:
    lines = [line[len(MARKER):] for line in log.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        return None
    try:
        return json.loads(lines[0])
    except json.JSONDecodeError:
        return None


def verify(log: str, contract: dict[str, Any], release_sha: str) -> dict[str, Any]:
    summary = _summary(log)
    if summary is None:
        return {"verdict": "FAIL", "failures": ["exactly one RAG_SMOKE_SUMMARY line is required"]}
    failures: list[str] = []

    def expect(name: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            failures.append(f"{name}: expected {wanted!r}, got {actual!r}")

    expect("release_sha", summary.get("release_sha"), release_sha)
    for key in ("corpus_revision", "chunk_count", "embedding_model", "embedding_revision",
                "embedding_dimensions", "published_rows", "publication_identity"):
        expect(key, summary.get(key), contract[key])
    target = summary.get("feast_target") or {}
    expect("feast_target.online_store", target.get("online_store"), contract["online_store"])
    if any(word in json.dumps(target) for word in contract["forbidden_redis_targets"]):
        failures.append("feast_target names a production Redis")
    validation = summary.get("validation") or {}
    expect("validation.readback_status", validation.get("readback_status"), contract["readback_status"])
    if not isinstance(validation.get("recall_at_k"), (int, float)) \
            or validation["recall_at_k"] < contract["min_recall_at_k"]:
        failures.append(f"validation.recall_at_k below {contract['min_recall_at_k']}: {validation.get('recall_at_k')}")
    if validation.get("citation_errors") != contract["max_citation_errors"]:
        failures.append(f"validation.citation_errors: {validation.get('citation_errors')}")
    if validation.get("empty_retrieval_rate") != contract["max_empty_retrieval_rate"]:
        failures.append(f"validation.empty_retrieval_rate: {validation.get('empty_retrieval_rate')}")
    return {"verdict": "FAIL" if failures else "PASS", "failures": failures, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=Path("jenkins/cd/rag-pipeline.contract.json"))
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = verify(args.log.read_text(encoding="utf-8"),
                    json.loads(args.contract.read_text(encoding="utf-8")), args.release_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "failures": result["failures"]}))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
