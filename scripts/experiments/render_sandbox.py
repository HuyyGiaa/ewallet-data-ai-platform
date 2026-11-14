#!/usr/bin/env python3
"""Render one EXP-C5-SANDBOX arm with its image digest."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "infra/kubernetes/agent-sandbox"


CAMPAIGN = re.compile(r"^agi-[0-9]{8}t[0-9]{6}z$")


def render(arm: str, image: str, campaign: str = "") -> str:
    """Return the arm's manifest; ``fleet`` also needs the campaign id (snapshot prefix, labels).

    Raises:
        ValueError: The image is not pinned by digest (Substrate rejects tags as well), or the
            fleet campaign id is not a lowercase ``agi-YYYYMMDDtHHMMSSz`` label value.
    """

    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("the agent image must be pinned by a full sha256 digest")
    text = (DIRECTORY / f"{'fleet' if arm == 'fleet' else 'arm-' + arm}.yaml.tmpl").read_text(encoding="utf-8")
    if arm == "fleet":
        if not CAMPAIGN.fullmatch(campaign):
            raise ValueError("fleet needs --campaign agi-YYYYMMDDtHHMMSSz (lowercase, a label value)")
        text = text.replace("__CAMPAIGN__", campaign)
    return text.replace("__AGENT_IMAGE__", image)


def main(argv: list[str] | None = None) -> int:
    """Print one arm."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=("baseline", "optimized", "fleet"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--campaign", default="", help="fleet only: snapshot prefix and labels")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.arm, args.image, args.campaign))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
