"""Compile the canonical F9 KFP definition without contacting a cluster."""

from __future__ import annotations

import argparse
from pathlib import Path

from kfp import compiler

from ai_platform.ml.pipelines.pipeline import f9_canonical_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml"),
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    compiler.Compiler().compile(
        pipeline_func=f9_canonical_pipeline,
        package_path=str(args.output),
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
