#!/usr/bin/env python3
"""Build a deterministic, non-destructive evidence migration planning map.

The script reads the read-only audit inventory and writes planning artifacts only.
It never reads artifact contents and never copies, moves, renames, or deletes an
evidence file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
SOURCE_TYPES = {".py"}
EXTERNAL_TYPES = {".parquet", ".pkl", ".db", ".zstd", ".gz"}
LARGE_LIMIT_BYTES = 10 * 1024 * 1024


def slug(value: str, fallback: str = "unknown") -> str:
    value = value.strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9.-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-.")
    return value or fallback


def path_identity(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def source_identity(row: dict[str, str]) -> str:
    """Return the stable identity captured before path sanitization."""
    captured = row.get("source_identity_sha256", "").strip().lower()
    if captured:
        if not valid_hash(captured):
            raise ValueError(f"invalid source_identity_sha256 for {row['current_path']}: {captured}")
        return captured
    return path_identity(row["current_path"])


def valid_hash(value: str) -> bool:
    return bool(HASH_RE.fullmatch(value.strip().lower()))


def is_not_evidence(row: dict[str, str]) -> bool:
    path = row["current_path"].lower()
    if row["file_type"].lower() in SOURCE_TYPES:
        return True
    return any(token in path for token in ("/__pycache__/", "/build-context-", "/site-packages/"))


def is_external_large(row: dict[str, str]) -> bool:
    try:
        size = int(row["size_bytes"])
    except ValueError:
        size = 0
    return row["file_type"].lower() in EXTERNAL_TYPES or size > LARGE_LIMIT_BYTES


def infer_component(row: dict[str, str]) -> str:
    path = row["current_path"].lower()
    workstream = slug(row["source_workstream"])
    if row["edai_phase"] == "EDAI-1":
        if workstream == "datahub":
            return "datahub"
        if workstream == "streaming":
            return "streaming"
        if workstream == "storage":
            return "storage"
        if workstream == "dp2":
            return "dp2"
        if workstream == "dp3":
            return "dp3"
        if workstream in {"airflow", "spark"}:
            if any(token in path for token in ("dp3", "feature", "historical")):
                return "dp3"
            if any(token in path for token in ("dp2", "silver", "gold")):
                return "dp2"
            if any(token in path for token in ("dp1", "bronze", "source-to-bronze")):
                return "dp1"
            return "orchestration" if workstream == "airflow" else "performance"
        return workstream
    if row["edai_phase"] == "EDAI-2":
        return {
            "agent": "agents",
            "agents": "agents",
            "ml": "mlops",
            "feast": "mlops",
        }.get(workstream, workstream)
    return "shared"


def resolved_component(row: dict[str, str]) -> str:
    """Use the accepted audit inference when path sanitization hides tokens."""
    return row.get("inferred_component", "").strip() or infer_component(row)


def infer_stage(path: str) -> str:
    lowered = path.lower()
    for stage in ("bronze", "silver", "gold"):
        if stage in lowered:
            return stage
    if any(token in lowered for token in ("feature", "historical", "dp3")):
        return "features"
    return "general"


def infer_variant(path: str) -> str:
    lowered = path.lower()
    if "baseline" in lowered:
        return "baseline"
    if "optimized" in lowered or "optimised" in lowered:
        return "optimized"
    return "common"


def infer_category(row: dict[str, str]) -> str:
    ext = row["file_type"].lower()
    if ext in {".png", ".jpg"}:
        return "screenshots"
    if ext in {".json", ".jsonl", ".yaml"}:
        return "api"
    if ext in {".log", ".txt", ".stderr", ".err", ".attempted", ".done", ".exit_code"}:
        return "logs"
    if ext in {".md", ".csv", ".tsv", ".html", ".xml", ".mmd"}:
        return "reports"
    if ext in EXTERNAL_TYPES:
        return "data"
    if ext == ".sha256":
        return "checksums"
    return "artifacts"


def infer_datahub_area(path: str) -> str:
    lowered = path.lower()
    if "assert" in lowered or "quality" in lowered or "validation" in lowered:
        return "assertions"
    if any(token in lowered for token in ("schema", "dataset", "inventory", "table")):
        return "datasets_schema"
    return "lineage"


def run_token(row: dict[str, str]) -> str:
    if row["run_id"] and row["run_id"] != "UNKNOWN":
        return slug(row["run_id"], "unknown-run")
    return f"source-{source_identity(row)[:12]}"


def artifact_filename(row: dict[str, str]) -> str:
    name = PurePosixPath(row["current_path"]).name or "artifact"
    return slug(name, f"artifact-{source_identity(row)[:12]}")


def base_destination(row: dict[str, str], external: bool = False) -> str:
    phase = "edai1" if row["edai_phase"] == "EDAI-1" else "edai2"
    component = resolved_component(row)
    run = run_token(row)
    stage = infer_stage(row["current_path"])
    variant = infer_variant(row["current_path"])
    category = infer_category(row)
    filename = artifact_filename(row)
    root = "EXTERNAL_DURABLE_STORE_REQUIRED" if external else f"docs/evidence/{phase}"
    if component == "datahub":
        area = infer_datahub_area(row["current_path"])
        channel = "screenshots" if category == "screenshots" else "api"
        return f"{root}/datahub/{area}/{channel}/{run}/{filename}"
    return f"{root}/{component}/{run}/{stage}/{variant}/{category}/{filename}"


def original_collision_classes(rows: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, int]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["proposed_destination"]].append(row)
    classes: dict[str, str] = {}
    counts = Counter()
    for key, members in groups.items():
        if len(members) == 1:
            kind = "NO_ORIGINAL_COLLISION"
        else:
            hashes = [m["sha256"].strip().lower() for m in members]
            if all(valid_hash(value) for value in hashes):
                kind = "SAME_CONTENT_DUPLICATE" if len(set(hashes)) == 1 else "DIFFERENT_CONTENT_COLLISION"
            else:
                kind = "UNKNOWN_HASH"
            counts[f"{kind}_GROUPS"] += 1
            counts[f"{kind}_ROWS"] += len(members)
        classes[key] = kind
    counts["ORIGINAL_COLLISION_GROUPS"] = sum(1 for members in groups.values() if len(members) > 1)
    counts["ORIGINAL_COLLISION_ROWS"] = sum(len(members) for members in groups.values() if len(members) > 1)
    return classes, dict(counts)


def reference_index(repo_root: Path, source_paths: list[str]) -> dict[str, str]:
    candidates = [p for p in source_paths if not Path(p).is_absolute()]
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in candidates:
        by_name[PurePosixPath(path).name].append(path)
    hits: dict[str, set[str]] = defaultdict(set)
    text_suffixes = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".txt"}
    for file_path in repo_root.rglob("*"):
        if not file_path.is_file() or ".git" in file_path.parts:
            continue
        if file_path.suffix.lower() not in text_suffixes and file_path.name not in {"Jenkinsfile", "Dockerfile"}:
            continue
        try:
            if file_path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ref = file_path.relative_to(repo_root).as_posix()
        for name, paths in by_name.items():
            if name not in text:
                continue
            for source in paths:
                if source in text and ref != source:
                    hits[source].add(ref)
    result = {}
    for source in source_paths:
        refs = hits.get(source, set())
        if not refs:
            result[source] = "DOCUMENTATION_ONLY" if source.startswith("docs/evidence/") else "UNKNOWN"
        elif any(ref.startswith("tests/") for ref in refs):
            result[source] = "REFERENCED_BY_TESTS"
        elif any(ref.startswith((".github/", "jenkins/")) or PurePosixPath(ref).name == "Jenkinsfile" for ref in refs):
            result[source] = "REFERENCED_BY_CI_CD"
        elif any(ref.startswith("infra/") for ref in refs):
            result[source] = "REFERENCED_BY_INFRASTRUCTURE"
        elif any(not (ref.startswith("docs/") or ref.endswith(".md")) for ref in refs):
            result[source] = "REFERENCED_BY_CODE"
        else:
            result[source] = "REFERENCED_BY_DOCUMENTATION"
    return result


def build(rows: list[dict[str, str]], repo_root: Path) -> tuple[list[dict[str, str]], dict[str, object]]:
    collision_class, original_counts = original_collision_classes(rows)
    uncaptured_paths = [row["current_path"] for row in rows if not row.get("path_reference_class", "").strip()]
    references = reference_index(repo_root, uncaptured_paths)
    references.update(
        {
            row["current_path"]: row["path_reference_class"].strip()
            for row in rows
            if row.get("path_reference_class", "").strip()
        }
    )
    mapped: list[dict[str, str]] = []
    provisional: dict[str, list[int]] = defaultdict(list)

    for row in rows:
        path = row["current_path"]
        identity = source_identity(row)
        artifact_id = f"ev-{identity[:16]}"
        classification = collision_class[row["proposed_destination"]]
        no_evidence = is_not_evidence(row)
        external = is_external_large(row)
        sensitive_unknown = row["sha256"] == "SKIPPED_POTENTIALLY_SENSITIVE_NAME"
        shared_unknown = row["edai_phase"] == "SHARED_OR_UNVERIFIED"
        active_owner = row["storage_scope"] == "active_worktree_only"

        if no_evidence:
            disposition = "NOT_EVIDENCE"
            destination = ""
            storage_class = "SOURCE_OR_BUILD_ASSET"
            owner_reason = "Implementation source/build-context file; keep with source or build provenance, not under evidence."
            effective_class = "MISCLASSIFIED_ARTIFACT"
        elif sensitive_unknown:
            disposition = "BLOCKED_FOR_REVIEW"
            destination = ""
            storage_class = "RESTRICTED_REVIEW"
            owner_reason = "Potentially sensitive name; content/checksum was intentionally not inspected."
            effective_class = classification
        elif shared_unknown:
            disposition = "BLOCKED_FOR_REVIEW"
            destination = ""
            storage_class = "PHASE_CLASSIFICATION_REQUIRED"
            owner_reason = "EDAI phase is unverified; owner must classify before selecting an approved taxonomy path."
            effective_class = classification
        elif external:
            disposition = "EXTERNAL_REFERENCE"
            destination = base_destination(row, external=True)
            storage_class = "EXTERNAL_DURABLE"
            owner_reason = "Large/binary artifact remains outside Git; durable URI, object version, checksum, and retrieval instructions are required."
            effective_class = "EXTERNAL_LARGE_ARTIFACT"
        elif active_owner:
            disposition = "KEEP_IN_PLACE"
            destination = base_destination(row)
            storage_class = "ACTIVE_OWNER_CHECKPOINT_REQUIRED"
            owner_reason = "Artifact exists only in an active worktree; proposed destination is planning-only pending owner checkpoint."
            effective_class = classification
        else:
            disposition = "PLANNED"
            destination = base_destination(row)
            storage_class = "GIT_COMPACT"
            owner_reason = "Planning destination only; Phase B1 approval and source-path dependency review are still required."
            effective_class = classification

        verification = "VERIFIED_SHA256" if valid_hash(row["sha256"]) else "UNKNOWN_HASH"
        item = {
            "artifact_id": artifact_id,
            "source_identity_sha256": identity,
            "original_source_path": path,
            "original_proposed_destination": row["proposed_destination"],
            "proposed_destination": destination,
            "edai_phase": row["edai_phase"],
            "component": resolved_component(row),
            "rubric_row": row["related_rubric_row"],
            "git_commit_sha": row["git_sha"],
            "run_id": row["run_id"],
            "execution_timestamp": row["execution_timestamp"],
            "artifact_type": row["file_type"],
            "artifact_size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "evidence_status": "PLANNING_ONLY_NOT_MIGRATED",
            "historical_current_classification": row["historical_or_current"],
            "storage_class": storage_class,
            "reproduction_runbook": "UNKNOWN" if row["reproducible"] == "UNKNOWN" else "COMPANION_METADATA_REQUIRES_OWNER_REVIEW",
            "migration_disposition": disposition,
            "migration_risk": row["move_risk"],
            "verification_status": verification,
            "collision_classification": effective_class,
            "original_collision_key": row["proposed_destination"] if classification != "NO_ORIGINAL_COLLISION" else "",
            "source_scope": row["storage_scope"],
            "tracked_by_git": row["tracked_by_git"],
            "ignored": row["ignored"],
            "path_reference_class": references[path],
            "owner_review_reason": owner_reason,
            "canonical_artifact_id": row.get("canonical_artifact_id", "").strip(),
        }
        mapped.append(item)
        if destination:
            provisional[destination].append(len(mapped) - 1)

    # Add a stable source-derived directory to every provisional destination
    # collision. This preserves run/stage/profile/basename while preventing any
    # overwrite. The same input always produces the same identity.
    for destination, indexes in provisional.items():
        if len(indexes) < 2:
            continue
        for index in indexes:
            item = mapped[index]
            parts = destination.rsplit("/", 1)
            item["proposed_destination"] = f"{parts[0]}/source-{item['source_identity_sha256'][:12]}/{parts[1]}"

    # Preserve traceability for verified same-content duplicates without
    # deleting or coalescing source records.
    same_hash_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for item in mapped:
        if item["collision_classification"] == "SAME_CONTENT_DUPLICATE" and valid_hash(item["sha256"]):
            same_hash_groups[(item["original_collision_key"], item["sha256"])].append(item)
    for members in same_hash_groups.values():
        captured = {item["canonical_artifact_id"] for item in members if item["canonical_artifact_id"]}
        if len(captured) > 1:
            raise ValueError(f"conflicting captured canonical artifact IDs: {sorted(captured)}")
        canonical = next(iter(captured), min(members, key=lambda item: item["source_identity_sha256"])["artifact_id"])
        for item in members:
            item["canonical_artifact_id"] = canonical

    destinations = [item["proposed_destination"] for item in mapped if item["proposed_destination"]]
    duplicate_destinations = {key: value for key, value in Counter(destinations).items() if value > 1}
    summary = {
        "schema_version": "1.0",
        "source_rows_processed": len(rows),
        **original_counts,
        "corrected_destination_collision_count": len(duplicate_destinations),
        "corrected_destination_collision_rows": sum(duplicate_destinations.values()),
        "migration_dispositions": dict(sorted(Counter(item["migration_disposition"] for item in mapped).items())),
        "collision_classifications": dict(sorted(Counter(item["collision_classification"] for item in mapped).items())),
        "verification_statuses": dict(sorted(Counter(item["verification_status"] for item in mapped).items())),
        "path_reference_classes": dict(sorted(Counter(item["path_reference_class"] for item in mapped).items())),
        "manual_review_items": sum(item["migration_disposition"] in {"BLOCKED_FOR_REVIEW", "KEEP_IN_PLACE"} for item in mapped),
        "external_artifact_references": sum(item["migration_disposition"] == "EXTERNAL_REFERENCE" for item in mapped),
        "not_evidence_items": sum(item["migration_disposition"] == "NOT_EVIDENCE" for item in mapped),
        "status": "PASS" if not duplicate_destinations else "FAIL",
        "phase_b0_only": True,
        "artifacts_migrated": 0,
    }
    return mapped, summary


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(__file__).with_name("EVIDENCE_INVENTORY_SNAPSHOT.csv"),
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--map-output", type=Path, required=True)
    parser.add_argument("--validation-output", type=Path, required=True)
    args = parser.parse_args()

    with args.inventory.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    mapped, summary = build(rows, args.repo_root.resolve())
    write_csv(args.map_output, mapped)
    args.validation_output.parent.mkdir(parents=True, exist_ok=True)
    args.validation_output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
