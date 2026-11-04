#!/usr/bin/env python3
"""Fail-closed static validation for the E-Wallet observability source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
MANIFEST = ROOT / "docs/evidence/observability/source_validation/manifest.json"


def historical_sha256(source_sha: str, path: str) -> str:
    """Hash the artifact at the immutable source commit named by the manifest."""

    content = subprocess.run(
        ["git", "show", f"{source_sha}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(content).hexdigest()


def validate() -> dict[str, object]:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    collector = (CHART / "templates/otel-collector.yaml").read_text()
    stack = (CHART / "templates/observability-stack.yaml").read_text()
    dashboards = [
        json.loads((CHART / "dashboards" / name).read_text())
        for name in ("platform-overview.json", "llm-runtime.json", "agent-mcp.json")
    ]
    expressions = "\n".join(
        target["expr"] for dashboard in dashboards
        for panel in dashboard["panels"] for target in panel["targets"]
    )
    manifest = json.loads(MANIFEST.read_text())
    checks = {
        "stack_opt_in": values["observabilityStack"]["enabled"] is False,
        "digest_pinned_images": all(
            re.search(r"@sha256:[0-9a-f]{64}$", values["observabilityStack"][name]["image"])
            for name in ("prometheus", "loki", "tempo", "grafana")
        ),
        "collector_memory_limiter": "memory_limiter" in collector and "limit_percentage: 80" in collector,
        "collector_sensitive_fields_deleted": all(
            key in collector for key in (
                "http.request.header.authorization", "gen_ai.prompt", "gen_ai.completion",
                "gen_ai.input.messages", "gen_ai.output.messages",
            )
        ),
        "single_backends": all(name in stack for name in ("prometheus", "loki", "tempo", "grafana")),
        "three_dashboards": len(dashboards) == 3,
        "missing_not_zero": "vector(0)" not in expressions and " or " not in expressions,
        "manifest_hashes": all(
            historical_sha256(manifest["artifact_bundle_sha"], item["path"]) == item["sha256"]
            for item in manifest["artifacts"]
        ),
    }
    if not all(checks.values()):
        raise SystemExit(json.dumps({"status": "STATIC_VALIDATION_FAILED", "checks": checks}, sort_keys=True))
    return {"status": "STATIC_VALIDATION_PASS", "gke_runtime_verified": False,
            "rubric_verified": False, "checks": checks}


if __name__ == "__main__":
    print(json.dumps(validate(), sort_keys=True))
