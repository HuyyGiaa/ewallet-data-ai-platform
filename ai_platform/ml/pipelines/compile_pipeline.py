"""Compile the canonical F9 KFP definition without contacting a cluster."""

from __future__ import annotations

import argparse
from pathlib import Path

from kfp import compiler

from ai_platform.ml.pipelines.pipeline import (
    configure_runtime_image,
    f9_canonical_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml"),
    )
    parser.add_argument(
        "--runtime-image",
        required=True,
        help="Explicit non-latest image used by every KFP runtime component.",
    )
    args = parser.parse_args()
    configure_runtime_image(args.runtime_image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    compiler.Compiler().compile(
        pipeline_func=f9_canonical_pipeline,
        package_path=str(args.output),
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
