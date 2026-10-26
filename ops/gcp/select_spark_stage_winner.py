"""Write an immutable winner artifact for one exact Method 2 stage pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from data_platform.config.spark_benchmark import (
    load_pair_manifest,
    select_stage_winner,
)


def _load(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--baseline-evidence", type=Path, required=True)
    parser.add_argument("--optimized-evidence", type=Path, required=True)
    parser.add_argument("--equivalence-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    artifact = select_stage_winner(
        pair_manifest=load_pair_manifest(args.pair_manifest),
        baseline_evidence=_load(args.baseline_evidence),
        candidate_evidence=_load(args.optimized_evidence),
        equivalence_evidence=_load(args.equivalence_evidence),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(artifact, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
