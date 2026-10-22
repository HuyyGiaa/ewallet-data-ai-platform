"""Verify that an immutable GCS Delta run contains the exact table set."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable, Sequence


def list_tables(
    root: str,
    *,
    runner: Callable[..., object] = subprocess.run,
) -> set[str]:
    completed = runner(
        ["gcloud", "storage", "ls", root.rstrip("/") + "/"],
        check=True,
        text=True,
        capture_output=True,
    )
    return {
        line.rstrip("/").rsplit("/", 1)[-1]
        for line in completed.stdout.splitlines()
        if line.strip()
    }


def verify_delta_run(
    root: str,
    expected_tables: set[str],
    *,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    if not root.startswith("gs://") or "/lakehouse/runs/" not in root:
        raise ValueError("root must be an immutable run-scoped gs:// path")
    actual = list_tables(root, runner=runner)
    if actual != expected_tables:
        raise ValueError(
            f"Delta table set mismatch at {root}: "
            f"expected={sorted(expected_tables)}, actual={sorted(actual)}"
        )
    for table in sorted(expected_tables):
        completed = runner(
            [
                "gcloud", "storage", "ls",
                f"{root.rstrip('/')}/{table}/_delta_log/*.json",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise ValueError(f"Delta log missing for {table} at {root}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--expected-table", action="append", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    verify_delta_run(args.root, set(args.expected_table))
    print(f"PASS: exact Delta table set at {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
