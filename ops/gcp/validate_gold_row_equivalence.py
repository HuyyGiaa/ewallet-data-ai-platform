"""Compare two Gold outputs row by row and write immutable JSON artifacts.

Read-only on both outputs. Roots may be local Delta copies or ``gs://`` URIs, and no
Dataproc batch is needed. Run it in the Feast environment, which has ``deltalake``:

    uv run --locked --project ai_platform/features/feast python \
        -m ops.gcp.validate_gold_row_equivalence --baseline-root ... \
        --optimized-root ... --table feat_user_90d --table opt_merchant_performance \
        --exact-equivalence dp2-gold-equivalence.json --output-dir out/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from data_platform.quality.gold_equivalence import (
    GOLD_POLICIES,
    DeltaSource,
    compare_table,
    final_verdict,
    row_report,
)


def write_once(path: Path, value: Mapping[str, object]) -> None:
    """Create a JSON artifact, refusing to replace an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")


def run(args: argparse.Namespace) -> int:
    """Compare the requested tables and return 0 only for a clean result."""

    results = []
    identity: dict[str, object] = {
        "baseline_root": args.baseline_root,
        "optimized_root": args.optimized_root,
        "delta_versions": {},
    }
    for table in args.table:
        if table not in GOLD_POLICIES:
            raise ValueError(f"no comparison policy for {table}")
        baseline = DeltaSource(f"{args.baseline_root.rstrip('/')}/{table}")
        optimized = DeltaSource(f"{args.optimized_root.rstrip('/')}/{table}")
        identity["delta_versions"][table] = {"baseline": baseline.version, "optimized": optimized.version}
        result = compare_table(table, baseline, optimized, GOLD_POLICIES[table])
        print(json.dumps({"table": table, "passed": result["passed"]}), flush=True)
        results.append(result)
    report = row_report(results, identity)
    write_once(args.output_dir / "dp2-gold-row-equivalence.json", report)
    if args.exact_equivalence is None:
        print(report["status"])
        return 0 if report["status"] == "PASS" else 1
    exact = json.loads(args.exact_equivalence.read_text(encoding="utf-8"))
    verdict = final_verdict(exact, report)
    write_once(args.output_dir / "dp2-gold-final-equivalence.json", verdict)
    print(verdict["status"])
    return 0 if verdict["status"] != "NOT_EQUIVALENT" else 1


def parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""

    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--baseline-root", required=True)
    result.add_argument("--optimized-root", required=True)
    result.add_argument("--table", action="append", required=True)
    result.add_argument("--exact-equivalence", type=Path)
    result.add_argument("--output-dir", type=Path, required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the comparison."""

    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
