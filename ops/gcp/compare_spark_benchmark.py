"""Generate JSON/CSV comparisons from comparable Spark A/B evidence."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Mapping


STAGES = ("dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features")


def percentage_change(baseline: float, optimized: float) -> float | None:
    if baseline <= 0:
        return None
    return (baseline - optimized) / baseline * 100


def comparison_row(
    baseline: Mapping[str, object],
    optimized: Mapping[str, object],
    equivalence: Mapping[str, object],
) -> dict[str, object]:
    for key in ("benchmark_id", "dataset_version", "git_sha", "stage"):
        if baseline.get(key) != optimized.get(key):
            raise ValueError(f"cannot compare different {key}")
    if baseline.get("spark_profile") != "baseline":
        raise ValueError("baseline evidence must declare spark_profile=baseline")
    if optimized.get("spark_profile") != "optimized":
        raise ValueError("optimized evidence must declare spark_profile=optimized")
    if baseline.get("runtime_version") != "2.2" or optimized.get(
        "runtime_version"
    ) != "2.2":
        raise ValueError("both batches must use runtime 2.2")
    for evidence in (baseline, optimized):
        properties = evidence.get("runtime_properties")
        if not isinstance(properties, Mapping) or properties.get(
            "dataproc.tier"
        ) != "standard":
            raise ValueError("both batches must use the Standard tier")
    if baseline.get("batch_state") != "SUCCEEDED" or optimized.get("batch_state") != "SUCCEEDED":
        raise ValueError("both batches must succeed before comparison")
    if equivalence.get("output_equivalent") is not True:
        raise ValueError("outputs are not equivalent")
    for key in ("benchmark_id", "dataset_version", "git_sha", "stage"):
        if equivalence.get(key) != baseline.get(key):
            raise ValueError(f"equivalence evidence has different {key}")
    baseline_runtime = float(baseline["wall_clock_seconds"])
    optimized_runtime = float(optimized["wall_clock_seconds"])
    baseline_dcu = float(baseline["dcu_hours"])
    optimized_dcu = float(optimized["dcu_hours"])
    runtime_pct = percentage_change(baseline_runtime, optimized_runtime)
    dcu_pct = percentage_change(baseline_dcu, optimized_dcu)
    return {
        "benchmark_id": baseline["benchmark_id"],
        "dataset_version": baseline["dataset_version"],
        "git_sha": baseline["git_sha"],
        "stage": baseline["stage"],
        "baseline_runtime_seconds": baseline_runtime,
        "optimized_runtime_seconds": optimized_runtime,
        "runtime_change_pct": runtime_pct,
        "runtime_result": "improvement" if runtime_pct is not None and runtime_pct >= 0 else "regression",
        "baseline_dcu_hours": baseline_dcu,
        "optimized_dcu_hours": optimized_dcu,
        "dcu_change_pct": dcu_pct,
        "dcu_result": "improvement" if dcu_pct is not None and dcu_pct >= 0 else "regression",
        "baseline_shuffle_gb_seconds": baseline["shuffle_storage_gb_seconds"],
        "optimized_shuffle_gb_seconds": optimized["shuffle_storage_gb_seconds"],
        "output_equivalent": True,
    }


def build_comparison(
    evidence_by_stage: Mapping[
        str, tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]
    ],
) -> dict[str, object]:
    rows = []
    identity: tuple[object, object, object] | None = None
    for stage in STAGES:
        try:
            baseline, optimized, equivalence = evidence_by_stage[stage]
        except KeyError as exc:
            raise ValueError(f"missing comparison evidence for {stage}") from exc
        row = comparison_row(baseline, optimized, equivalence)
        current = (
            row["benchmark_id"], row["dataset_version"], row["git_sha"]
        )
        if identity is None:
            identity = current
        elif current != identity:
            raise ValueError(
                "all DP1/DP2/DP3 comparison stages must share benchmark, "
                "dataset and Git SHA"
            )
        rows.append(row)
    return {
        "benchmark_id": identity[0],
        "dataset_version": identity[1],
        "git_sha": identity[2],
        "stages": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    evidence_by_stage = {}
    for stage in STAGES:
        load = lambda path: json.loads(path.read_text(encoding="utf-8"))
        evidence_by_stage[stage] = (
            load(args.evidence_root / "baseline" / f"{stage}.json"),
            load(args.evidence_root / "optimized" / f"{stage}.json"),
            load(args.evidence_root / "equivalence" / f"{stage}.json"),
        )
    payload = build_comparison(evidence_by_stage)
    rows = payload["stages"]
    (args.evidence_root / "comparison.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with (args.evidence_root / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
