#!/usr/bin/env python3
"""Render one EXP-C5-SANDBOX arm with its image digest."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "infra/kubernetes/agent-sandbox"


def render(arm: str, image: str) -> str:
    """Return the arm's manifest.

    Raises:
        ValueError: The image is not pinned by digest; Substrate rejects tags as well.
    """

    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("the agent image must be pinned by a full sha256 digest")
    return (DIRECTORY / f"arm-{arm}.yaml.tmpl").read_text(encoding="utf-8").replace("__AGENT_IMAGE__", image)


def main(argv: list[str] | None = None) -> int:
    """Print one arm."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=("baseline", "optimized"))
    parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.arm, args.image))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
