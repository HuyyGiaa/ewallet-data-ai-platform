#!/usr/bin/env bash
# Bounded CD handler for agents: runs scripts/ci/bounded_cd.py with its contract.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "$root"
exec uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.bounded_cd \
  --contract jenkins/cd/agents.contract.json --prompt-registry "${PROMPT_REGISTRY_URL:-}" ${K8S_API:+--api "$K8S_API"} "$@"
