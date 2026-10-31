"""Deterministically map changed paths to independently deployable components."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PATH_COMPONENTS = {
    "ai_platform/llm_agent/rag/": {"rag-pipeline"},
    "ai_platform/features/feast/feature_repo/rag_": {"rag-pipeline"},
    "data/llm_agent/rag/": {"rag-pipeline"},
    "ops/llm_agent/rag_": {"rag-pipeline"},
    "ops/llm_agent/run_rag_": {"rag-pipeline"},
    "ops/llm_agent/verify_rag_": {"rag-pipeline"},
    "ai_platform/llm_agent/": {"feature-rag-mcp", "drift-mcp", "mcp-gateway"},
    "infra/docker/llm-agent-tooling/": {"feature-rag-mcp", "drift-mcp", "platform-tooling"},
    "ops/llm_agent/registry_catalog": {"agent-registry"},
    "ops/llm_agent/": {"platform-tooling"},
    "infra/helm/llm-agent-platform/": {"platform-routing"},
    "infra/helm/llm-agent-platform/prompts/": {
        "fraud-coordinator",
        "feature-rag-specialist",
        "drift-specialist",
        "agent-registry",
    },
    "infra/helm/llm-agent-platform/templates/agents.yaml": {
        "fraud-coordinator",
        "feature-rag-specialist",
        "drift-specialist",
    },
    "infra/helm/llm-agent-platform/templates/policy-gateways.yaml": {"mcp-gateway", "a2a-gateway"},
    "infra/helm/llm-d/": {"llm-d-router"},
    "infra/helm/agentregistry/": {"agent-registry"},
    "infra/helm/keda/": {"agent-autoscaling"},
    "infra/kubernetes/kagent-platform/": {
        "fraud-coordinator",
        "feature-rag-specialist",
        "drift-specialist",
    },
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
        for prefix, names in sorted(PATH_COMPONENTS.items(), key=lambda item: -len(item[0])):
            if path.startswith(prefix):
                components.update(names)
                break
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
