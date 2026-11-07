#!/usr/bin/env python3
"""Check Jenkins' production rights against the reviewed matrix with ``kubectl auth can-i``.

Read-only: ``can-i`` asks the API server a question and changes nothing. Run it before the
proposed RBAC is applied (``--state before``: every production right must be absent) and
after (``--state after``: rights must match the matrix exactly).

    python scripts/ci/rbac_matrix.py --context "$KUBE_CONTEXT" --state before
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "jenkins/cd/production-rbac-matrix.json"
Ask = Callable[[Sequence[str]], str]


def command(context: str, subject: str, row: Mapping[str, Any]) -> list[str]:
    """Build the ``can-i`` command for one matrix row."""

    resource = f"{row['resource']}/{row['name']}" if row["name"] else row["resource"]
    scope = ["-n", row["namespace"]] if row["namespace"] else []
    return ["kubectl", "--context", context, "auth", "can-i", row["verb"], resource, *scope, f"--as={subject}"]


def ask_cluster(arguments: Sequence[str]) -> str:
    """Run ``can-i``; it exits non-zero for "no", so the answer is read from its output."""

    result = subprocess.run(list(arguments), capture_output=True, text=True, timeout=30, check=False)
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "error"


def expected(row: Mapping[str, Any], state: str) -> bool:
    """Return the expected answer: before approval, no production right exists."""

    return bool(row["after"]) if state == "after" else False


def evaluate(matrix: Mapping[str, Any], context: str, state: str, ask: Ask) -> list[str]:
    """Return one line per row whose answer differs; an unreadable answer is a difference."""

    problems = []
    for row in matrix["rows"]:
        arguments = command(context, matrix["subject"], row)
        answer = ask(arguments)
        want = "yes" if expected(row, state) else "no"
        if answer != want:
            problems.append(f"{' '.join(arguments[4:-1])}: got {answer}, want {want}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Print the matrix result; exit 1 on any difference."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state", choices=("before", "after"), required=True)
    args = parser.parse_args(argv)
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    problems = evaluate(matrix, args.context, args.state, ask_cluster)
    for problem in problems:
        print(problem)
    print(f"{len(matrix['rows']) - len(problems)}/{len(matrix['rows'])} rows match state {args.state}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
