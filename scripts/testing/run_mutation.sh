#!/usr/bin/env bash
# B1.3: mutmut over the bounded scope in tests/quality/mutmut.cfg.
# Usage: scripts/testing/run_mutation.sh [output-directory]
# Environment:
#   B1_MUTATION_WORKDIR  Workspace outside the repository (default: a temp dir).
#   B1_MUTATION_CHILDREN Parallel mutant workers (default: 6).
#   B1_MUTATION_TARGET   Optional mutant glob, e.g. "ai_platform.llm_agent.*".

source "$(dirname "${BASH_SOURCE[0]}")/b1_common.sh"
b1_require_uv

output="${1:-$B1_EVIDENCE_ROOT/mutation}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
b1_record_provenance "$output"

workspace="${B1_MUTATION_WORKDIR:-$(mktemp -d "${TMPDIR:-/tmp}/ewallet-b1-mutation.XXXXXX")}"
children="${B1_MUTATION_CHILDREN:-6}"
target="${B1_MUTATION_TARGET:-}"
echo "mutation workspace: $workspace"

# Tracked plus new, never ignored: this leaves out .venv, caches and evidence.
mkdir -p "$workspace"
git -C "$REPO_ROOT" ls-files -z --cached --others --exclude-standard -- \
    ai_platform data_platform/__init__.py data_platform/config \
    data/llm_agent/rag configs/experiments/ml-ab tests \
  | (cd "$REPO_ROOT" && tar --null --ignore-failed-read -T - -cf -) \
  | tar -xf - -C "$workspace"
cp "$REPO_ROOT/tests/quality/mutmut.cfg" "$workspace/setup.cfg"

cd "$workspace"
export HYPOTHESIS_PROFILE=b1-mutation
run_status=0
b1_uv_run test mutmut run --max-children "$children" ${target:+"$target"} \
  2>&1 | tee "$output/run.txt" || run_status=$?

b1_uv_run test mutmut results --all true >"$output/results.txt" 2>&1 || true
b1_uv_run test mutmut export-cicd-stats >/dev/null 2>&1 || true
cp mutants/mutmut-cicd-stats.json "$output/" 2>/dev/null || true

b1_uv_run test python "$REPO_ROOT/scripts/testing/mutation_report.py" \
  --results "$output/results.txt" \
  --equivalents "$REPO_ROOT/tests/quality/equivalent_mutants.txt" \
  --output "$output/summary"
echo "workspace kept for 'mutmut show <name>': $workspace"
exit "$run_status"
