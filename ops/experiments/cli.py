"""Command line for experiment preflight, comparison and checklists.

    python -m ops.experiments.cli preflight --spec s.json --dir out --kube-context c \
        --namespace ewallet-bench --setting image=...@sha256:...
    python -m ops.experiments.cli compare --spec s.json --dir out
    python -m ops.experiments.cli finish --spec s.json --dir out --deviation "run 2 stopped"
    python -m ops.experiments.cli checklist --spec s.json
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ops.experiments.adapters import load_arm
from ops.experiments.compare import compare
from ops.experiments.preflight import capture, finish_manifest, write_preflight
from ops.experiments.report import render_checklist, render_markdown, write_comparison
from ops.experiments.spec import load_spec


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return an exit code."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("preflight", "compare", "checklist", "finish"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--kube-context", default="")
    parser.add_argument("--namespace", default="ewallet-bench")
    parser.add_argument("--setting", action="append", default=[], help="name=value")
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument("--deviation", action="append", default=[])
    args = parser.parse_args(argv)
    spec = load_spec(args.spec)
    if args.command == "checklist":
        print(render_checklist(spec), end="")
        return 0
    if args.dir is None:
        parser.error("--dir is required")
    if args.command == "preflight":
        if not args.kube_context:
            parser.error("--kube-context is required; the kubeconfig default is never used")
        settings = dict(item.split("=", 1) for item in args.setting)
        write_preflight(spec, capture(spec, kube_context=args.kube_context, namespace=args.namespace,
                                      settings=settings, fixtures=args.fixture), args.dir)
        return 0
    if args.command == "finish":
        finish_manifest(args.dir, args.deviation)
        return 0
    report = compare(spec, load_arm(args.dir / "baseline"), load_arm(args.dir / "optimized"))
    write_comparison(report, args.dir)
    print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
