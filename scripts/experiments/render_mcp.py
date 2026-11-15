#!/usr/bin/env python3
"""Render the isolated MCP experiment for one arm.

The arm table is the whole difference between the arms. Both arms use the same two images
and the same probe. ``--print-hash`` gives the load profile hash used as a control.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.experiments.render_bench import dns_peers  # noqa: E402  (one DNS peer rule for every experiment)

# NodeLocal DNSCache listener and kube-dns ClusterIP of ewallet-dev (infra/helm/llm-agent-platform/values-gke-cpu.yaml).
GKE_DNS_IPS = "169.254.20.10,34.118.224.10"
TEMPLATE = ROOT / "infra/kubernetes/experiments/mcp-exp.yaml.tmpl"
PROBE = ROOT / "ops/experiments/mcp_probe.py"
ARMS = {"baseline": {"REPLICAS": "1", "ATTEMPTS": "1"}, "optimized": {"REPLICAS": "2", "ATTEMPTS": "3"}}
DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})$")


def profile_hash(duration: int, rate: str) -> str:
    """Hash the probe and its offered load; arm settings are excluded on purpose."""

    return hashlib.sha256(PROBE.read_bytes() + f"{duration}/{rate}".encode()).hexdigest()


def render(arm: str, mcp_image: str, probe_image: str, release_sha: str, duration: int, rate: str,
           dns_ips: str = GKE_DNS_IPS) -> str:
    """Return the manifest with the probe script embedded.

    Raises:
        ValueError: An image is not pinned by digest.
    """

    digest = DIGEST.search(mcp_image)
    if digest is None or not DIGEST.search(probe_image):
        raise ValueError("images must be pinned by a full sha256 digest")
    values = {**ARMS[arm], "DNS_PEERS": dns_peers(dns_ips), "MCP_IMAGE": mcp_image, "MCP_DIGEST": digest.group(1), "PROBE_IMAGE": probe_image,
              "RELEASE_SHA": release_sha, "DURATION": str(duration), "RATE": rate, "JOB_DEADLINE": str(duration + 120)}
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    script = "".join(f"    {line}\n" if line else "\n" for line in PROBE.read_text(encoding="utf-8").splitlines())
    return text.replace('  mcp_probe.py: ""\n', f"  mcp_probe.py: |\n{script}")


def main(argv: list[str] | None = None) -> int:
    """Print the manifest or the load profile hash."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=sorted(ARMS))
    parser.add_argument("--mcp-image", default="")
    parser.add_argument("--probe-image", default="")
    parser.add_argument("--release-sha", default="")
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--rate", default="2")
    parser.add_argument("--dns-ips", default=GKE_DNS_IPS,
                        help="NodeLocal DNSCache address and kube-dns ClusterIP, comma separated (values-gke-cpu.yaml)")
    parser.add_argument("--print-hash", action="store_true")
    args = parser.parse_args(argv)
    if args.print_hash:
        print(profile_hash(args.duration, args.rate))
        return 0
    try:
        sys.stdout.write(render(args.arm, args.mcp_image, args.probe_image, args.release_sha, args.duration, args.rate,
                                args.dns_ips))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
