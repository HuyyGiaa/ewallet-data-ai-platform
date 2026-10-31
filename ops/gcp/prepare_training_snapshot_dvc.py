"""Plan or prepare DVC metadata for an immutable Training Snapshot."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Sequence

from ai_platform.ml.training.dvc_revision import write_revision_descriptor


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-evidence", type=Path, required=True)
    parser.add_argument("--descriptor-output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="ML-2 only: validate evidence and create the descriptor with O_EXCL semantics",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dvc_command = shlex.join(("dvc", "add", str(args.descriptor_output)))
    if not args.execute:
        plan = {
            "schema_version": "training-snapshot-dvc-plan-v1",
            "status": "PLAN_ONLY",
            "publication_evidence": str(args.publication_evidence),
            "descriptor_output": str(args.descriptor_output),
            "data_copy": False,
            "dvc_command_after_descriptor_creation": dvc_command,
        }
        print("DRY RUN — no descriptor or DVC metadata was created")
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    descriptor = write_revision_descriptor(
        args.publication_evidence, args.descriptor_output
    )
    print(json.dumps(descriptor, indent=2, sort_keys=True))
    print(f"NEXT | {dvc_command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
