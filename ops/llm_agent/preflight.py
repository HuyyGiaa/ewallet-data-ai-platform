"""Source-only Gate A preflight. It performs no cluster or cloud mutation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from ai_platform.llm_agent.config import ARCHITECTURE_CONTRACT, load_release_config


FORBIDDEN_PATTERNS = {
    "nginx ingress": re.compile(r"(?i)nginx(?:[-_ ]ingress|\.ingress)"),
    "standalone A2A gateway": re.compile(r"(?i)(?:standalone|independent)[-_ ]a2a[-_ ]gateway"),
    "Kubernetes Ingress": re.compile(r"(?m)^kind:\s*Ingress\s*$"),
}


def _render(chart: Path, values: Path) -> str:
    result = subprocess.run(
        ["helm", "template", "llm-agent", str(chart), "--values", str(values)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def validate_source(root: Path) -> dict[str, object]:
    release = load_release_config(
        root / "configs/llm_agent/release-v1.json", repository_root=root
    )
    chart = root / "infra/helm/llm-agent-platform"
    renders = {
        environment: _render(chart, chart / f"values-{environment}.yaml")
        for environment in ("dev", "gcp")
    }
    combined = "\n".join(renders.values())
    for name, pattern in FORBIDDEN_PATTERNS.items():
        if pattern.search(combined):
            raise ValueError(f"render contains forbidden {name}")
    gcp = renders["gcp"]
    required = (
        "gateway.networking.k8s.io/v1",
        "kind: Gateway",
        "kind: HTTPRoute",
        "istio.io/dataplane-mode: ambient",
        "security.istio.io/v1",
        "kind: AuthorizationPolicy",
        "kagent-native-a2a",
        "policy-only",
        "modelconfig-agentgateway-vllm",
        "vllm-openai-cpu:v0.32.0-x86_64@sha256:",
        "agentgateway:v1.7.0@sha256:",
        "kind: ModelConfig",
        "ewallet-fraud-llm",
        "--max-num-queued-reqs",
    )
    missing = [value for value in required if value not in gcp]
    if missing:
        raise ValueError(f"gcp Helm render is missing {missing}")
    versions = (root / "infra/kubernetes/kagent-pilot/versions.env").read_text()
    for pin in ("KAGENT_VERSION=0.10.3", "SUBSTRATE_VERSION=0.0.9"):
        if pin not in versions:
            raise ValueError(f"missing approved runtime pin {pin}")
    return {
        "gate": "A",
        "status": "SOURCE_ONLY_PASS",
        "release_id": release.release_id,
        "architecture": dict(ARCHITECTURE_CONTRACT),
        "component_count": len(release.components),
        "rendered_environments": sorted(renders),
        "runtime_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    result = validate_source(args.repository_root.resolve())
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
