#!/usr/bin/env python3
"""Build the I7 documentation registry without changing documentation files.

The accepted 99-row Phase A registry is retained as an immutable classification
snapshot. Documents added by I2-I6 are discovered deterministically and receive
UNKNOWN status until an owner performs row-level architecture/evidence review.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


FIELDS = (
    "document_path",
    "edai_phase",
    "component",
    "document_purpose",
    "status",
    "authoritative_reference",
    "related_rubric_rows",
    "related_implementation_branch",
    "evidence_reference",
    "proposed_future_location",
    "reason_for_classification",
)


def phase(path: str) -> str:
    if path.startswith(("docs/edai1/", "docs/streaming/")):
        return "EDAI1"
    if path.startswith(("docs/edai2/", "docs/ml/", "docs/llm_agent/", "docs/agent/")):
        return "EDAI2"
    return "SHARED_OR_UNKNOWN"


def component(path: str) -> str:
    for prefix, name in (
        ("docs/streaming/", "streaming"),
        ("docs/ml/", "mlops"),
        ("docs/llm_agent/", "llm_agent"),
        ("docs/agent/", "agents"),
        ("docs/observability", "observability"),
        ("docs/cloud/jenkins", "cicd"),
        ("docs/edai1/DATAHUB", "datahub"),
    ):
        if path.startswith(prefix):
            return name
    return "shared"


def title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("_", " ").replace("-", " ").strip()


def documentation_paths(path_inventory: Path) -> list[str]:
    paths = [
        line.strip()
        for line in path_inventory.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate path in documentation path inventory")
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument(
        "--paths",
        type=Path,
        default=Path(__file__).with_name("DOCUMENTATION_PATHS_I7.txt"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.repo_root.resolve()
    with args.snapshot.open(encoding="utf-8", newline="") as handle:
        snapshot = list(csv.DictReader(handle))
    if tuple(snapshot[0]) != FIELDS:
        raise ValueError("unexpected Phase A snapshot schema")

    rows = {row["document_path"]: row for row in snapshot}
    for path in documentation_paths(args.paths):
        if path in rows:
            continue
        rows[path] = {
            "document_path": path,
            "edai_phase": phase(path),
            "component": component(path),
            "document_purpose": title(root / path),
            "status": "UNKNOWN",
            "authoritative_reference": (
                "Frozen Architecture; original EDAI K11.xlsx; "
                "Living Decisions require row-level review"
            ),
            "related_rubric_rows": "UNKNOWN",
            "related_implementation_branch": (
                "integration/final-integration-20261101@"
                "4801300ec47d59d0e6b854b5b82b3cb9e4a474b3 (I6 baseline)"
            ),
            "evidence_reference": (
                "UNKNOWN; correlate with docs/rubric/rubric_evidence_matrix.csv"
            ),
            "proposed_future_location": path,
            "reason_for_classification": (
                "Added after the accepted Phase A snapshot; status remains UNKNOWN "
                "until architecture, source, runtime evidence, and rubric linkage are reviewed."
            ),
        }

    missing = sorted(path for path in rows if not (root / path).is_file())
    if missing:
        raise ValueError(f"registry snapshot paths absent from checkout: {missing}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows[path] for path in sorted(rows))
    print(f"registry_rows={len(rows)} snapshot_rows={len(snapshot)} added_rows={len(rows)-len(snapshot)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
