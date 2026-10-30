"""Fail-closed validation for the two active EDAI rubric sheets.

This module deliberately uses only the Python standard library.  It validates
claims; it never contacts cloud services or converts source-only work into
runtime credit.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = "edai-rubric-evidence-matrix-v1"
EVIDENCE_SCHEMA_VERSION = "edai-rubric-runtime-evidence-v1"
STATUSES = frozenset({"OPEN", "PARTIAL", "VERIFIED"})
ASSESSMENT_BASES = frozenset({"VERIFIED", "INFERRED"})
TRUE_VALUES = frozenset({"true", "1", "yes"})
FALSE_VALUES = frozenset({"false", "0", "no", ""})
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

REQUIRED_COLUMNS = (
    "sheet",
    "row",
    "points",
    "requirement",
    "status",
    "source_worktree",
    "source_sha",
    "source_paths",
    "tests",
    "environment",
    "deployment",
    "timestamp",
    "screenshots",
    "logs",
    "queries",
    "metrics",
    "baseline_metrics",
    "optimized_metrics",
    "correctness_evidence",
    "runtime_evidence",
    "optimization_required",
    "architecture_conflict",
    "equivalence_decision",
    "missing_proof",
    "next_safe_command",
    "assessment_basis",
    "awarded_points",
    "notes",
)


class MatrixValidationError(ValueError):
    """Raised when the evidence matrix could overstate or omit rubric credit."""


@dataclass(frozen=True)
class RubricExpectation:
    sheet: str
    row: int
    points: int
    optimization_required: bool = False


EDAI_1_POINTS = (
    2, 3, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 2, 2, 3, 3, 2, 3, 3,
    2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 10, 10,
)
# Column E of rows 3..57 in docs/rubric/edai_k11_workbook_extract.json.
EDAI_2_POINTS = (
    2, 2, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 2, 2, 1, 1, 8,
    2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    1, 1, 1, 1, 1, 1, 1, 2, 2, 1, 1, 1, 2, 2, 2,
)

# Rubric rows whose wording explicitly requires a measured optimization or A/B
# result.  Other rows may still need measurements according to their evidence
# narrative, but these can never be VERIFIED without matched metrics.
OPTIMIZATION_ROWS = frozenset(
    {
        ("edai-1 (50%)", 4),
        ("edai-1 (50%)", 12),
        ("edai-1 (50%)", 13),
        ("edai-1 (50%)", 14),
        ("edai-1 (50%)", 17),
        ("edai-1 (50%)", 18),
        ("edai-1 (50%)", 20),
        ("edai-1 (50%)", 21),
        ("edai-2 (50%)", 6),
        ("edai-2 (50%)", 27),
        ("edai-2 (50%)", 52),
        ("edai-2 (50%)", 53),
    }
)


def _build_expected_rows() -> tuple[RubricExpectation, ...]:
    rows: list[RubricExpectation] = []
    for sheet, start, points in (
        ("edai-1 (50%)", 3, EDAI_1_POINTS),
        ("edai-2 (50%)", 3, EDAI_2_POINTS),
    ):
        for offset, value in enumerate(points):
            row = start + offset
            rows.append(
                RubricExpectation(
                    sheet,
                    row,
                    value,
                    optimization_required=(sheet, row) in OPTIMIZATION_ROWS,
                )
            )
    return tuple(rows)


EXPECTED_RUBRIC = _build_expected_rows()


def _boolean(value: str, *, field: str, key: tuple[str, int]) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise MatrixValidationError(f"{key}: {field} must be true or false")


def _require_nonempty(row: Mapping[str, str], fields: Iterable[str], key: tuple[str, int]) -> None:
    missing = [field for field in fields if not row.get(field, "").strip()]
    if missing:
        raise MatrixValidationError(f"{key}: VERIFIED row missing {', '.join(missing)}")


def load_matrix(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise MatrixValidationError(
                f"matrix is missing required columns: {', '.join(missing)}"
            )
        return [dict(row) for row in reader]


def validate_matrix(
    rows: Sequence[Mapping[str, str]],
    *,
    expected: Sequence[RubricExpectation] = EXPECTED_RUBRIC,
) -> dict[str, object]:
    expected_by_key = {(item.sheet, item.row): item for item in expected}
    if len(expected_by_key) != len(expected):
        raise MatrixValidationError("validator expectation contains duplicate rubric rows")

    seen: dict[tuple[str, int], Mapping[str, str]] = {}
    status_counts: Counter[str] = Counter()
    awarded_by_sheet: defaultdict[str, int] = defaultdict(int)
    available_by_sheet: defaultdict[str, int] = defaultdict(int)

    for expectation in expected:
        available_by_sheet[expectation.sheet] += expectation.points

    for item in rows:
        try:
            key = (item["sheet"].strip(), int(item["row"]))
            points = int(item["points"])
            awarded = int(item["awarded_points"])
        except (KeyError, TypeError, ValueError) as error:
            raise MatrixValidationError(f"invalid rubric row identity or points: {error}") from error

        if key in seen:
            raise MatrixValidationError(f"duplicate rubric row: {key}")
        seen[key] = item
        expectation = expected_by_key.get(key)
        if expectation is None:
            raise MatrixValidationError(f"unexpected rubric row: {key}")
        if points != expectation.points:
            raise MatrixValidationError(
                f"{key}: points {points} do not match active rubric {expectation.points}"
            )
        if not item.get("requirement", "").strip():
            raise MatrixValidationError(f"{key}: requirement is empty")

        status = item.get("status", "").strip().upper()
        if status not in STATUSES:
            raise MatrixValidationError(f"{key}: unsupported status {status!r}")
        basis = item.get("assessment_basis", "").strip().upper()
        if basis not in ASSESSMENT_BASES:
            raise MatrixValidationError(f"{key}: unsupported assessment_basis {basis!r}")
        optimization = _boolean(
            item.get("optimization_required", ""),
            field="optimization_required",
            key=key,
        )
        if optimization != expectation.optimization_required:
            raise MatrixValidationError(
                f"{key}: optimization_required does not match the active rubric contract"
            )
        conflict = _boolean(
            item.get("architecture_conflict", ""),
            field="architecture_conflict",
            key=key,
        )
        if conflict and not item.get("equivalence_decision", "").strip():
            raise MatrixValidationError(
                f"{key}: architecture_conflict requires equivalence_decision"
            )

        if status != "VERIFIED":
            if awarded != 0:
                raise MatrixValidationError(
                    f"{key}: {status} rows require zero awarded points"
                )
            if not item.get("missing_proof", "").strip():
                raise MatrixValidationError(f"{key}: {status} row missing missing_proof")
            if not item.get("next_safe_command", "").strip():
                raise MatrixValidationError(f"{key}: {status} row missing next_safe_command")
        else:
            if basis != "VERIFIED":
                raise MatrixValidationError(f"{key}: VERIFIED status cannot use INFERRED basis")
            if awarded != points:
                raise MatrixValidationError(
                    f"{key}: VERIFIED row must award exactly {points} points"
                )
            _require_nonempty(
                item,
                (
                    "source_worktree",
                    "source_sha",
                    "source_paths",
                    "tests",
                    "environment",
                    "deployment",
                    "timestamp",
                    "correctness_evidence",
                    "runtime_evidence",
                ),
                key,
            )
            if not GIT_SHA_RE.fullmatch(item["source_sha"].strip()):
                raise MatrixValidationError(f"{key}: VERIFIED source_sha must be a full Git SHA")
            if not UTC_TIMESTAMP_RE.fullmatch(item["timestamp"].strip()):
                raise MatrixValidationError(
                    f"{key}: VERIFIED timestamp must be UTC ISO-8601"
                )
            if optimization:
                _require_nonempty(
                    item,
                    ("baseline_metrics", "optimized_metrics", "metrics"),
                    key,
                )

        status_counts[status] += 1
        awarded_by_sheet[key[0]] += awarded

    missing = sorted(set(expected_by_key) - set(seen))
    if missing:
        labels = ", ".join(f"{sheet} row {row}" for sheet, row in missing)
        raise MatrixValidationError(f"missing rubric rows: {labels}")

    expected_totals = defaultdict(int)
    for expectation in expected:
        expected_totals[expectation.sheet] += expectation.points
    for sheet, total in expected_totals.items():
        if expected is EXPECTED_RUBRIC and total != 100:
            raise MatrixValidationError(f"{sheet}: active rubric total is {total}, expected 100")

    sheet_summaries = {
        sheet: {
            "available_points": available_by_sheet[sheet],
            "awarded_points": awarded_by_sheet[sheet],
            "weighted_points": round(awarded_by_sheet[sheet] * 0.5, 2),
        }
        for sheet in sorted(available_by_sheet)
    }
    return {
        "row_count": len(rows),
        "available_points": sum(available_by_sheet.values()),
        "awarded_points": sum(awarded_by_sheet.values()),
        "weighted_score": round(sum(awarded_by_sheet.values()) * 0.5, 2),
        "status_counts": {name: status_counts.get(name, 0) for name in ("VERIFIED", "PARTIAL", "OPEN")},
        "sheets": sheet_summaries,
    }


def render_matrix_json(
    rows: Sequence[Mapping[str, str]],
    *,
    expected: Sequence[RubricExpectation] = EXPECTED_RUBRIC,
) -> str:
    summary = validate_matrix(rows, expected=expected)
    normalized_rows = [dict(row) for row in rows]
    document = {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "rows": normalized_rows,
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate_source_bindings(rows: Sequence[Mapping[str, str]]) -> None:
    """Verify that recorded source paths exist in the recorded Git commit.

    The current checkout may have advanced beyond the evidence-producing commit,
    so this checks the commit object rather than requiring HEAD equality.
    """

    tree_cache: dict[tuple[str, str], tuple[str, ...]] = {}
    for item in rows:
        sha = item.get("source_sha", "").strip()
        worktree_value = item.get("source_worktree", "").strip()
        paths_value = item.get("source_paths", "").strip()
        if not sha and not worktree_value and not paths_value:
            continue
        key_label = (item.get("sheet", ""), item.get("row", ""))
        if not (sha and worktree_value):
            raise MatrixValidationError(
                f"{key_label}: source binding requires both source_worktree and source_sha"
            )
        if not GIT_SHA_RE.fullmatch(sha):
            raise MatrixValidationError(f"{key_label}: source_sha must be a full Git SHA")
        worktree = Path(worktree_value)
        if not worktree.is_dir():
            raise MatrixValidationError(f"{key_label}: source_worktree does not exist: {worktree}")
        cache_key = (str(worktree), sha)
        if cache_key not in tree_cache:
            commit = subprocess.run(
                ["git", "-C", str(worktree), "cat-file", "-e", f"{sha}^{{commit}}"],
                capture_output=True,
                text=True,
            )
            if commit.returncode:
                raise MatrixValidationError(
                    f"{key_label}: source_sha {sha} is not present in {worktree}"
                )
            listing = subprocess.run(
                ["git", "-C", str(worktree), "ls-tree", "-r", "--name-only", sha],
                check=True,
                capture_output=True,
                text=True,
            )
            tree_cache[cache_key] = tuple(listing.stdout.splitlines())
        tree = tree_cache[cache_key]
        for raw_path in paths_value.split(";"):
            source_path = raw_path.strip().rstrip("/")
            if not source_path:
                continue
            matched = any(
                candidate == source_path
                or candidate.startswith(source_path + "/")
                or fnmatch.fnmatchcase(candidate, source_path)
                for candidate in tree
            )
            if not matched:
                raise MatrixValidationError(
                    f"{key_label}: source path {source_path!r} is not present at {sha}"
                )


def build_evidence_template(
    *,
    rubric_id: str,
    run_id: str,
    git_sha: str,
    worktree: str,
    benchmark: bool,
) -> dict[str, object]:
    if not RUN_ID_RE.fullmatch(rubric_id):
        raise ValueError("rubric_id must be a bounded stable identifier")
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be a bounded stable identifier")
    if not GIT_SHA_RE.fullmatch(git_sha):
        raise ValueError("git_sha must be a 40-character lowercase Git SHA")
    if not worktree.strip():
        raise ValueError("worktree must not be empty")
    document: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "rubric_id": rubric_id,
        "run_id": run_id,
        "identity": {
            "git_sha": git_sha,
            "worktree": worktree,
            "dataset_version": "",
            "artifact_or_image_digest": "",
        },
        "execution": {
            "started_at_utc": "",
            "finished_at_utc": "",
            "environment": "",
            "command": "",
            "exit_code": None,
        },
        "correctness": {"status": "NOT_RUN", "evidence": []},
        "artifacts": {"logs": [], "screenshots": [], "queries": []},
        "status": "NOT_RUN",
    }
    if benchmark:
        document["benchmark"] = {
            "workload_identity": "",
            "baseline": {"identity": "", "metrics": {}},
            "optimized": {"identity": "", "metrics": {}},
            "correctness_equivalence": "NOT_RUN",
            "trade_offs": [],
        }
    return document


def write_evidence_template(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as error:
        raise FileExistsError(f"refusing to overwrite existing evidence: {path}") from error
