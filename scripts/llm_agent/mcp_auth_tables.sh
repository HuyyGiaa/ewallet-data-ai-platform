#!/usr/bin/env bash
# Obsolete MCP server auth tables (mcp-auth-* Secrets): audit and exact, gated deletion.
#
#   mcp_auth_tables.sh plan <out-dir>     [READ-ONLY] token-free inventory (ops/llm_agent/auth_table_inventory.py)
#                                         and the KEEP/DELETE/UNKNOWN matrix (ops/llm_agent/auth_table_matrix.py),
#                                         compared with the reviewed list EXPECT_DELETE; exit 1 on any difference
#   mcp_auth_tables.sh delete <out-dir>   [MUTATES] needs LC_APPROVAL=AUTH-TABLES:<LC_CAMPAIGN>; re-plans from the
#                                         live cluster, requires the computed list to equal EXPECT_DELETE, runs a
#                                         server-side dry run, deletes exactly those Secrets by name, then re-plans:
#                                         the tables must be gone and every final table still verified
#
# Env: CTX (kube context), LC_CAMPAIGN, EXPECT_DELETE (space-separated <namespace>/<secret> reviewed list),
#      PYTHON (default python3; the modules are stdlib only), KUBECTL, HISTORY=0 to skip the Helm history scan.
# Never prints or stores a credential value: the inventory keeps credential ids, subjects and statuses only.
set -euo pipefail
shopt -s inherit_errexit
command=${1:-} out=${2:-}
die() { echo "mcp_auth_tables: FAIL: $*" >&2; exit 1; }
[[ "$command" == plan || "$command" == delete ]] && [[ -n "$out" ]] || { sed -n '2,14p' "$0"; exit 2; }
: "${CTX:?CTX is required}" "${LC_CAMPAIGN:?LC_CAMPAIGN is required}"
[[ -n "${EXPECT_DELETE+x}" ]] || die "EXPECT_DELETE (the reviewed deletion list, possibly empty) is required"
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PYTHON=${PYTHON:-python3} KUBECTL=${KUBECTL:-kubectl}
umask 077; mkdir -p "$out"
read -r -a expected <<< "$EXPECT_DELETE"
for item in "${expected[@]}"; do
  [[ "$item" =~ ^(ewallet-mcp|ewallet-agents)/mcp-auth-[a-z0-9-]+$ ]] || die "EXPECT_DELETE entry $item is not an MCP auth table"
done

plan() {   # plan <label> <expected...>: writes <label>-inventory.json and <label>-matrix.{json,tsv}
  local label=$1; shift
  local history=(); [[ "${HISTORY:-1}" == 0 ]] && history=(--skip-history)
  (cd "$ROOT" && "$PYTHON" -m ops.llm_agent.auth_table_inventory --context "$CTX" --out "$out/$label-inventory.json" "${history[@]}")
  (cd "$ROOT" && "$PYTHON" -m ops.llm_agent.auth_table_matrix --inventory "$out/$label-inventory.json" \
     --out "$out/$label-matrix.json" --expect-delete "$@")
}

case "$command" in
  plan) plan plan "${expected[@]}" ;;
  delete)
    [[ "${LC_APPROVAL:-}" == "AUTH-TABLES:${LC_CAMPAIGN}" ]] || die "approval AUTH-TABLES:${LC_CAMPAIGN} is required (LC_APPROVAL)"
    (( ${#expected[@]} > 0 )) || die "nothing to delete"
    plan before-delete "${expected[@]}" > /dev/null || die "the live matrix differs from the reviewed list; nothing deleted"
    for item in "${expected[@]}"; do
      "$KUBECTL" --context "$CTX" -n "${item%%/*}" delete secret "${item#*/}" --dry-run=server >> "$out/delete-dry-run.txt"
    done
    for item in "${expected[@]}"; do
      echo "$(date -u +%FT%TZ) MUTATION delete secret $item" >> "$out/delete.log"
      "$KUBECTL" --context "$CTX" -n "${item%%/*}" delete secret "${item#*/}" --wait=true >> "$out/delete.txt"
    done
    HISTORY=0 plan after-delete > /dev/null || die "after deletion the matrix is not clean (see $out/after-delete-matrix.tsv)"
    for item in "${expected[@]}"; do
      [[ -z "$("$KUBECTL" --context "$CTX" -n "${item%%/*}" get secret "${item#*/}" --ignore-not-found -o name)" ]] \
        || die "$item still exists"
    done
    echo "deleted ${#expected[@]} auth table(s); final tables verified (see $out/after-delete-matrix.tsv)" ;;
esac
