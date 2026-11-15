#!/usr/bin/env python3
"""Render one EXP-C5-SANDBOX arm with its image digest."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "infra/kubernetes/agent-sandbox"


# The AGI campaign id (scripts/llm_agent/agi_campaign.sh); also a valid label value and GCS prefix.
CAMPAIGN = re.compile(r"^agi-[0-9]{8}T[0-9]{6}Z$")
# The same rule as ops/llm_agent/g3_mcp_credentials.py release names.
CREDENTIAL_RELEASE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,22}[a-z0-9])?$")


def render(arm: str, image: str, campaign: str = "", credential_release: str = "") -> str:
    """Return the arm's manifest; ``fleet`` also needs the campaign id (snapshot prefix, labels)
    and the MCP credential release whose client Secrets it reads
    (``mcp-token-<subject>-<server>-<release>``, key ``token``, as g3_mcp_credentials writes them).

    Raises:
        ValueError: The image is not pinned by digest (Substrate rejects tags as well), or the
            fleet campaign id is not the AGI form ``agi-YYYYMMDDTHHMMSSZ``, or the
            credential release is not a 1..24 character lowercase DNS label.
    """

    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("the agent image must be pinned by a full sha256 digest")
    text = (DIRECTORY / f"{'fleet' if arm == 'fleet' else 'arm-' + arm}.yaml.tmpl").read_text(encoding="utf-8")
    if arm == "fleet":
        if not CAMPAIGN.fullmatch(campaign):
            raise ValueError("fleet needs --campaign agi-YYYYMMDDTHHMMSSZ (the AGI campaign id)")
        if not CREDENTIAL_RELEASE.fullmatch(credential_release):
            raise ValueError("fleet needs --credential-release <1..24 character lowercase DNS label>")
        text = text.replace("__CAMPAIGN__", campaign).replace("__CREDENTIAL_RELEASE__", credential_release)
    return text.replace("__AGENT_IMAGE__", image)


def main(argv: list[str] | None = None) -> int:
    """Print one arm."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=("baseline", "optimized", "fleet"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--campaign", default="", help="fleet only: snapshot prefix and labels")
    parser.add_argument("--credential-release", default="", help="fleet only: MCP credential release of the client Secrets")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.arm, args.image, args.campaign, args.credential_release))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
