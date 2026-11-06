# shellcheck shell=bash
# Sourced by the O1, G2 and G1 scripts, which were written for a cluster with no platform
# on it. Their cleanup uninstalls the Helm release llm-agent in the default namespace and
# resizes cpu-pool. On today's cluster that release is production, so they refuse to run.
#
# The check fails closed: when Helm cannot answer, the script stops.

# refuse_production_release <kube-context>: returns 0 only when the release is proven absent.
refuse_production_release() {
  local releases
  if ! releases=$(helm list --kube-context "$1" --namespace default --all --short); then
    echo "production guard: cannot list Helm releases in $1; refusing to continue" >&2
    return 1
  fi
  if grep -qx llm-agent <<<"$releases"; then
    echo "production guard: release llm-agent exists in namespace default of $1." >&2
    echo "production guard: this script would uninstall it; use scripts/experiments instead." >&2
    return 1
  fi
}
