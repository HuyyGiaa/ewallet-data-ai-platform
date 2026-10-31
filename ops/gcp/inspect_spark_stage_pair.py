"""Read one validated field from an immutable Method 2 pair manifest."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from data_platform.config.spark_benchmark import load_pair_manifest, pair_member


def pair_field(path: Path, *, spark_profile: str, field: str) -> str:
    manifest = load_pair_manifest(path)
    if field == "benchmark_id":
        return str(manifest["benchmark_id"])
    if field == "pair_manifest_digest":
        return str(manifest["pair_manifest_digest"])
    return str(pair_member(manifest, spark_profile)[field])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument(
        "--spark-profile", choices=("baseline", "optimized"), required=True
    )
    parser.add_argument(
        "--field",
        choices=("benchmark_id", "pair_manifest_digest", "output_run_id", "output_root"),
        required=True,
    )
    args = parser.parse_args(argv)
    print(
        pair_field(
            args.pair_manifest,
            spark_profile=args.spark_profile,
            field=args.field,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
