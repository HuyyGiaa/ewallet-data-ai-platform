"""CLI for validating the EDAI evidence matrix and local evidence templates."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from ops.rubric.evidence_matrix import (
    build_evidence_template,
    load_matrix,
    render_matrix_json,
    validate_source_bindings,
    validate_matrix,
    write_evidence_template,
)


DEFAULT_MATRIX = Path("docs/rubric/rubric_evidence_matrix.csv")


def _git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate rubric claims or initialize a local evidence file."
    )
    result.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    result.add_argument("--json-output", type=Path)
    result.add_argument("--init-evidence", type=Path)
    result.add_argument("--rubric-id")
    result.add_argument("--run-id")
    result.add_argument("--git-sha")
    result.add_argument("--worktree")
    result.add_argument("--benchmark", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.init_evidence:
        if not args.rubric_id or not args.run_id:
            parser().error("--init-evidence requires --rubric-id and --run-id")
        git_sha = args.git_sha or _git_value("rev-parse", "HEAD")
        worktree = args.worktree or _git_value("rev-parse", "--show-toplevel")
        document = build_evidence_template(
            rubric_id=args.rubric_id,
            run_id=args.run_id,
            git_sha=git_sha,
            worktree=worktree,
            benchmark=args.benchmark,
        )
        write_evidence_template(args.init_evidence, document)
        print(json.dumps({"status": "CREATED", "path": str(args.init_evidence)}, sort_keys=True))
        return 0

    rows = load_matrix(args.matrix)
    summary = validate_matrix(rows)
    validate_source_bindings(rows)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(render_matrix_json(rows), encoding="utf-8")
    print(json.dumps({"status": "PASS", "summary": summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
