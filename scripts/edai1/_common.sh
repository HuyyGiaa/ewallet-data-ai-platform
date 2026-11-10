#!/usr/bin/env bash
# Shared helper for scripts/edai1/*.sh. Sourced, never executed directly.
# All logic lives in ops/gcp/edai1_campaign_cli.py; this file only finds the
# interpreter of the local Airflow 3 environment and runs it from the repository.

EDAI1_SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
EDAI1_REPOSITORY_ROOT=$(cd -- "${EDAI1_SCRIPT_DIR}/../.." && pwd)

edai1_python() {
  local candidate
  for candidate in "${EDAI1_PYTHON:-}" \
                   "${AIRFLOW_BIN:+$(dirname -- "${AIRFLOW_BIN}")/python}" \
                   "${HOME}/miniconda3/envs/airflow/bin/python"; do
    if [[ -n ${candidate} && -x ${candidate} ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  echo "ERROR: no Python from the Airflow environment found; set EDAI1_PYTHON or AIRFLOW_BIN." >&2
  return 2
}

# Run one subcommand of the campaign CLI with the arguments passed through unchanged.
edai1_cli() {
  local python
  python=$(edai1_python) || exit 2
  cd -- "${EDAI1_REPOSITORY_ROOT}"
  PYTHONPATH="${EDAI1_REPOSITORY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    exec "${python}" -m ops.gcp.edai1_campaign_cli "$@"
}

# Succeed when -h or --help is among the arguments.
edai1_help_requested() {
  local argument
  for argument in "$@"; do
    [[ ${argument} == -h || ${argument} == --help ]] && return 0
  done
  return 1
}
