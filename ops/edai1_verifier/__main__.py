"""Command line: verify one campaign's evidence and print the fingerprint.

Usage::

    python -m edai1_verifier --spec SPEC.json --evidence-dir DIR [--output FILE]

Writes the canonical report to ``--output`` (optional) and prints one summary line
of JSON with the verdict and the report SHA-256. Exit code 0 means PASS, 1 means
at least one check failed, 2 means the specification itself is invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .report import canonical_json, fingerprint, verify
from .spec import SpecError, load_spec


def main(argv: Sequence[str] | None = None) -> int:
    """Run the verification."""

    parser = argparse.ArgumentParser(prog="edai1_verifier", description=__doc__.splitlines()[0])
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
    except SpecError as exc:
        print(json.dumps({"verdict": "INVALID_SPEC", "error": str(exc)}), file=sys.stderr)
        return 2
    report = verify(args.evidence_dir, spec)
    text = canonical_json(report)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps({
        "verdict": report["verdict"],
        "checks": report["check_count"],
        "failed": len(report["failed_checks"]),
        "report_sha256": fingerprint(report),
    }, sort_keys=True))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
