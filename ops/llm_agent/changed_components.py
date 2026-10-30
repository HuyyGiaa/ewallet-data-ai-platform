"""Deterministically map changed paths to independently deployable components."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PATH_COMPONENTS = {
    "ai_platform/llm_agent/": {"feature-rag-mcp", "drift-mcp", "mcp-gateway"},
    "infra/helm/llm-agent-platform/": {"platform-routing"},
    "infra/kubernetes/kagent-pilot/": {
        "fraud-coordinator",
        "feature-rag-specialist",
        "drift-specialist",
    },
    "data_platform/orchestration/airflow/": {"rag-pipeline"},
    "data_platform/processing/flink/": {
        "streaming-to-offline",
        "streaming-to-online",
    },
}


def detect(paths: list[str]) -> list[str]:
    components: set[str] = set()
    for path in paths:
        for prefix, names in PATH_COMPONENTS.items():
            if path.startswith(prefix):
                components.update(names)
    return sorted(components)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = [line.strip() for line in args.changed_files.read_text().splitlines() if line.strip()]
    args.output.write_text(json.dumps({"components": detect(paths)}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
