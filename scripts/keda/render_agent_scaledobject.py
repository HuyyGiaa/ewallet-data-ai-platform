"""Print one agent's ScaledObject from the real chart, moved to a test namespace.

Renders with the KEDA overlay, so a local webhook check sees exactly what would deploy.
Transfer annotations are dropped: the test namespace has no HPA to adopt.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import yaml

CHART = Path(__file__).resolve().parents[2] / "infra/helm/llm-agent-platform"
OVERRIDES = ["--set", "agents.enabled=true", "--set", "agents.mode=agent",
             "--set", "agents.snapshotsLocation=gs://bucket/prefix/"]


def main() -> int:
    """Render the chart and print the requested object."""
    name, namespace = sys.argv[1], sys.argv[2]
    rendered = subprocess.run(
        ["helm", "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml"),
         "-f", str(CHART / "g3/keda.yaml"), *OVERRIDES,
         "--show-only", "templates/autoscaling.yaml"],
        check=True, capture_output=True, text=True,
    ).stdout
    item = next(d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "ScaledObject" and d["metadata"]["name"] == name)
    item["metadata"]["namespace"] = namespace
    item["metadata"].pop("annotations", None)
    item["spec"]["advanced"]["horizontalPodAutoscalerConfig"].pop("name", None)
    print(yaml.safe_dump(item))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
