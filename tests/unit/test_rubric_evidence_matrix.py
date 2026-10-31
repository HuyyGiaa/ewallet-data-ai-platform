from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.rubric.evidence_matrix import (
    MatrixValidationError,
    RubricExpectation,
    build_evidence_template,
    render_matrix_json,
    validate_source_bindings,
    validate_matrix,
    write_evidence_template,
)


EXPECTED = (
    RubricExpectation("edai-1 (50%)", 3, 2),
    RubricExpectation("edai-1 (50%)", 4, 3, optimization_required=True),
)


def valid_rows() -> list[dict[str, str]]:
    return [
        {
            "sheet": "edai-1 (50%)",
            "row": "3",
            "points": "2",
            "requirement": "Docker Compose",
            "status": "VERIFIED",
            "source_worktree": "/repo",
            "source_sha": "a" * 40,
            "source_paths": "infra/docker/compose.yml",
            "tests": "compose config PASS",
            "environment": "local Docker",
            "deployment": "compose project healthy",
            "timestamp": "2026-10-29T00:00:00Z",
            "screenshots": "evidence/compose.png",
            "logs": "evidence/compose.log",
            "queries": "docker compose ps",
            "metrics": "4/4 services healthy",
            "baseline_metrics": "",
            "optimized_metrics": "",
            "correctness_evidence": "health checks PASS",
            "runtime_evidence": "evidence/compose.json",
            "optimization_required": "false",
            "architecture_conflict": "false",
            "equivalence_decision": "",
            "missing_proof": "",
            "next_safe_command": "docker compose ps",
            "assessment_basis": "VERIFIED",
            "awarded_points": "2",
            "notes": "",
        },
        {
            "sheet": "edai-1 (50%)",
            "row": "4",
            "points": "3",
            "requirement": "Optimized Dockerfile and before/after sizes",
            "status": "PARTIAL",
            "source_worktree": "/repo",
            "source_sha": "b" * 40,
            "source_paths": "images/Dockerfile",
            "tests": "docker build source check PASS",
            "environment": "source-only",
            "deployment": "",
            "timestamp": "",
            "screenshots": "",
            "logs": "",
            "queries": "",
            "metrics": "",
            "baseline_metrics": "",
            "optimized_metrics": "",
            "correctness_evidence": "",
            "runtime_evidence": "",
            "optimization_required": "true",
            "architecture_conflict": "false",
            "equivalence_decision": "",
            "missing_proof": "matched image builds and sizes",
            "next_safe_command": "build bounded baseline and optimized images",
            "assessment_basis": "VERIFIED",
            "awarded_points": "0",
            "notes": "source exists; runtime proof absent",
        },
    ]


class RubricEvidenceMatrixTest(unittest.TestCase):
    def test_valid_matrix_awards_only_runtime_verified_rows(self) -> None:
        summary = validate_matrix(valid_rows(), expected=EXPECTED)

        self.assertEqual(5, summary["available_points"])
        self.assertEqual(2, summary["awarded_points"])
        self.assertEqual({"VERIFIED": 1, "PARTIAL": 1, "OPEN": 0}, summary["status_counts"])

    def test_missing_scored_row_is_rejected(self) -> None:
        with self.assertRaisesRegex(MatrixValidationError, "missing rubric rows.*row 4"):
            validate_matrix(valid_rows()[:1], expected=EXPECTED)

    def test_duplicate_scored_row_is_rejected(self) -> None:
        rows = valid_rows()
        rows.append(dict(rows[0]))
        with self.assertRaisesRegex(MatrixValidationError, "duplicate rubric row"):
            validate_matrix(rows, expected=EXPECTED)

    def test_source_only_row_cannot_receive_points(self) -> None:
        rows = valid_rows()
        rows[1]["awarded_points"] = "3"
        with self.assertRaisesRegex(MatrixValidationError, "PARTIAL.*zero awarded points"):
            validate_matrix(rows, expected=EXPECTED)

    def test_verified_row_requires_runtime_identity(self) -> None:
        rows = valid_rows()
        rows[0]["runtime_evidence"] = ""
        with self.assertRaisesRegex(MatrixValidationError, "VERIFIED.*runtime_evidence"):
            validate_matrix(rows, expected=EXPECTED)

    def test_verified_row_rejects_non_utc_timestamp(self) -> None:
        rows = valid_rows()
        rows[0]["timestamp"] = "yesterday"
        with self.assertRaisesRegex(MatrixValidationError, "UTC ISO-8601"):
            validate_matrix(rows, expected=EXPECTED)

    def test_verified_optimization_requires_matched_measurements(self) -> None:
        rows = valid_rows()
        rows[1].update(
            status="VERIFIED",
            deployment="image digests baseline/optimized",
            timestamp="2026-10-29T01:00:00Z",
            correctness_evidence="same image smoke test PASS",
            runtime_evidence="evidence/image-size.json",
            awarded_points="3",
        )
        with self.assertRaisesRegex(MatrixValidationError, "baseline_metrics"):
            validate_matrix(rows, expected=EXPECTED)

    def test_architecture_conflict_requires_explicit_equivalence_decision(self) -> None:
        rows = valid_rows()
        rows[1]["architecture_conflict"] = "true"
        with self.assertRaisesRegex(MatrixValidationError, "equivalence_decision"):
            validate_matrix(rows, expected=EXPECTED)

    def test_json_render_is_deterministic_and_contains_rows_and_summary(self) -> None:
        first = render_matrix_json(valid_rows(), expected=EXPECTED)
        second = render_matrix_json(valid_rows(), expected=EXPECTED)

        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertEqual("edai-rubric-evidence-matrix-v1", document["schema_version"])
        self.assertEqual(2, len(document["rows"]))
        self.assertEqual(2, document["summary"]["awarded_points"])

    def test_evidence_template_is_git_bound_and_refuses_overwrite(self) -> None:
        template = build_evidence_template(
            rubric_id="edai-1-row-4",
            run_id="docker-size-20261029T010000Z",
            git_sha="c" * 40,
            worktree="/repo",
            benchmark=True,
        )
        self.assertEqual("c" * 40, template["identity"]["git_sha"])
        self.assertIn("baseline", template["benchmark"])
        self.assertIn("optimized", template["benchmark"])

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            write_evidence_template(target, template)
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                write_evidence_template(target, template)

    def test_evidence_template_rejects_untrusted_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "40-character lowercase Git SHA"):
            build_evidence_template(
                rubric_id="edai-1-row-4",
                run_id="run-1",
                git_sha="main",
                worktree="/repo",
                benchmark=False,
            )

    def test_source_binding_rejects_path_missing_at_recorded_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            (repo / "present.txt").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "present.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
            sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            row = dict(valid_rows()[1])
            row.update(source_worktree=str(repo), source_sha=sha, source_paths="missing.txt")

            with self.assertRaisesRegex(MatrixValidationError, "missing.txt.*not present"):
                validate_source_bindings([row])


if __name__ == "__main__":
    unittest.main()
