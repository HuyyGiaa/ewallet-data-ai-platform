#!/usr/bin/env bash
# Thin entry point the shared Jenkins pipeline calls: verify a release plan and dispatch it.
# All policy lives in scripts/ci/release_dispatch.py (typed executors per release-unit kind,
# resource locks, fail-first, release verdict); this wrapper only selects the locked environment.
#   deploy_release.sh --plan <release-plan.json> [--execute] [--production-context <ctx>]
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$root"
exec uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.release_dispatch "$@"
