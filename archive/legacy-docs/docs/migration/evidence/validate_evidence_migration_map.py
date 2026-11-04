#!/usr/bin/env python3
"""Validate the Phase B0 map without touching evidence artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath


ALLOWED_DISPOSITIONS = {
    "PLANNED",
    "KEEP_IN_PLACE",
    "EXTERNAL_REFERENCE",
    "BLOCKED_FOR_REVIEW",
    "NOT_EVIDENCE",
}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
EXTERNAL_TYPES = {".parquet", ".pkl", ".db", ".zstd", ".gz"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mapping", type=Path)
    parser.add_argument("--expected-rows", type=int, default=1445)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    with args.mapping.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    errors: list[str] = []
    warnings: list[str] = []
    sources = Counter(row["original_source_path"] for row in rows)
    ids = Counter(row["artifact_id"] for row in rows)
    destinations = Counter(row["proposed_destination"] for row in rows if row["proposed_destination"])

    if len(rows) != args.expected_rows:
        errors.append(f"row_count={len(rows)} expected={args.expected_rows}")
    for source, count in sources.items():
        if count > 1:
            errors.append(f"duplicate_source_record:{source}:{count}")
    for artifact_id, count in ids.items():
        if count > 1:
            errors.append(f"duplicate_artifact_id:{artifact_id}:{count}")
    for destination, count in destinations.items():
        if count > 1:
            errors.append(f"duplicate_destination:{destination}:{count}")

    hashes_by_destination: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        disposition = row["migration_disposition"]
        destination = row["proposed_destination"]
        source = row["original_source_path"]
        source_identity = row.get("source_identity_sha256", "").strip().lower()
        if not HASH_RE.fullmatch(source_identity):
            errors.append(f"invalid_source_identity:{source}:{source_identity}")
        elif row["artifact_id"] != f"ev-{source_identity[:16]}":
            errors.append(f"artifact_id_source_identity_mismatch:{source}:{row['artifact_id']}")
        if disposition not in ALLOWED_DISPOSITIONS:
            errors.append(f"invalid_disposition:{source}:{disposition}")
        if row["evidence_status"] != "PLANNING_ONLY_NOT_MIGRATED":
            errors.append(f"invalid_phase_b0_status:{source}:{row['evidence_status']}")
        if disposition in {"PLANNED", "KEEP_IN_PLACE", "EXTERNAL_REFERENCE"} and not destination:
            errors.append(f"missing_destination:{source}:{disposition}")
        if disposition in {"BLOCKED_FOR_REVIEW", "NOT_EVIDENCE"} and destination:
            errors.append(f"unexpected_destination:{source}:{disposition}:{destination}")
        if destination:
            posix = PurePosixPath(destination)
            if posix.is_absolute() or ".." in posix.parts or str(posix) != destination:
                errors.append(f"unsafe_or_non_normalized_destination:{source}:{destination}")
            if disposition == "EXTERNAL_REFERENCE":
                if not destination.startswith("EXTERNAL_DURABLE_STORE_REQUIRED/"):
                    errors.append(f"invalid_external_prefix:{source}:{destination}")
            elif not destination.startswith("docs/evidence/"):
                errors.append(f"invalid_repository_taxonomy:{source}:{destination}")
            if row["edai_phase"] == "EDAI-1" and disposition != "EXTERNAL_REFERENCE" and not destination.startswith("docs/evidence/edai1/"):
                errors.append(f"wrong_edai1_destination:{source}:{destination}")
            if row["edai_phase"] == "EDAI-2" and disposition != "EXTERNAL_REFERENCE" and not destination.startswith("docs/evidence/edai2/"):
                errors.append(f"wrong_edai2_destination:{source}:{destination}")
            if row["run_id"] == "UNKNOWN" and f"source-{source_identity[:12]}" not in destination:
                errors.append(f"missing_source_identity_for_unknown_run:{source}:{destination}")
            value = row["sha256"].lower()
            if HASH_RE.fullmatch(value):
                hashes_by_destination[destination].add(value)
        if row["artifact_type"] == ".py" and disposition != "NOT_EVIDENCE":
            errors.append(f"python_source_classified_as_evidence:{source}:{disposition}")
        if row["artifact_type"] in EXTERNAL_TYPES and disposition not in {
            "EXTERNAL_REFERENCE",
            "BLOCKED_FOR_REVIEW",
            "NOT_EVIDENCE",
        }:
            errors.append(f"large_binary_not_external:{source}:{row['artifact_type']}:{disposition}")
        if row["verification_status"] == "UNKNOWN_HASH" and row["collision_classification"] == "SAME_CONTENT_DUPLICATE":
            errors.append(f"unknown_hash_deduplicated:{source}")
        if row["migration_disposition"] == "BLOCKED_FOR_REVIEW":
            warnings.append(f"manual_review:{source}:{row['owner_review_reason']}")

    for destination, hashes in hashes_by_destination.items():
        if len(hashes) > 1:
            errors.append(f"conflicting_hashes_share_destination:{destination}:{sorted(hashes)}")

    report = {
        "schema_version": "1.0",
        "mapping": str(args.mapping),
        "rows": len(rows),
        "unique_sources": len(sources),
        "unique_artifact_ids": len(ids),
        "nonempty_destinations": len(destinations),
        "duplicate_destination_count": sum(1 for count in destinations.values() if count > 1),
        "errors": errors,
        "warning_count": len(warnings),
        "manual_review_count": sum(row["migration_disposition"] in {"BLOCKED_FOR_REVIEW", "KEEP_IN_PLACE"} for row in rows),
        "dispositions": dict(sorted(Counter(row["migration_disposition"] for row in rows).items())),
        "collision_classifications": dict(sorted(Counter(row["collision_classification"] for row in rows).items())),
        "status": "PASS" if not errors else "FAIL",
        "phase_b0_only": True,
        "artifacts_migrated": 0,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
