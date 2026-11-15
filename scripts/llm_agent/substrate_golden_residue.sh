#!/usr/bin/env bash
# Residual golden actors: Substrate 0.0.9's ActorTemplate controller does nothing on deletion, so a
# deleted ActorTemplate leaves its golden actor in the reserved ate-golden atespace. This removes
# only reviewed residues, through the supported ate-api DeleteActor (kubectl-ate), never Valkey.
#
# usage: substrate_golden_residue.sh <list|delete> <reviewed-file> [evidence-dir]
#   list    [READ-ONLY]  kubectl-ate get actors --atespace ate-golden; marks each reviewed actor
#                        DELETABLE only if Suspended, without pod, snapshot or in-progress snapshot,
#                        and its ActorTemplate no longer exists
#   delete  [MUTATES]    RESIDUE_APPROVAL=GOLDEN-RESIDUE:<context>; re-checks each actor right before
#                        DeleteActor; anything not DELETABLE is refused, nothing outside the file
# Env: KUBE_CONTEXT, KUBECONFIG, KUBECTL, KUBECTL_ATE (path to kubectl-ate built from substrate v0.0.9).
set -euo pipefail
action=${1:-} reviewed=${2:-} evidence=${3:-}
die() { echo "substrate_golden_residue: FAIL: $*" >&2; exit 1; }
case "$action" in list|delete) ;; *) die "usage: substrate_golden_residue.sh <list|delete> <reviewed-file> [evidence-dir]";; esac
[[ -r "$reviewed" ]] || die "reviewed file required"
[[ -n "${KUBE_CONTEXT:-}" ]] || die "KUBE_CONTEXT is required"
ate=${KUBECTL_ATE:-kubectl-ate} kubectl=${KUBECTL:-kubectl}
command -v "$ate" > /dev/null || die "kubectl-ate not found (build it from the pinned substrate v0.0.9 source)"
if [[ -n "$evidence" ]]; then mkdir -p "$evidence"; else evidence=$(mktemp -d); fi
atespace=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["atespace"])' "$reviewed")
[[ "$atespace" == ate-golden ]] || die "only the reserved ate-golden atespace is in scope"

classify() {   # one line per reviewed actor: <id> <DELETABLE|KEEP> <reason>
  "$ate" --context "$KUBE_CONTEXT" get actors --atespace "$atespace" -o json > "$evidence/actors.json"
  python3 - "$reviewed" "$evidence/actors.json" <<'PY' > "$evidence/classified.txt"
import json, subprocess, sys, os
reviewed = json.load(open(sys.argv[1]))
raw = json.load(open(sys.argv[2]))
items = raw.get("actors", raw if isinstance(raw, list) else [])
live = {a.get("metadata", {}).get("name"): a for a in items}
for entry in reviewed["actors"]:
    actor = live.get(entry["id"])
    if actor is None:
        print(entry["id"], "KEEP", "not-found(already-gone)"); continue
    reasons = []
    if actor.get("status") != "STATUS_SUSPENDED": reasons.append(f"status={actor.get('status')}")
    if actor.get("ateomPodName"): reasons.append("has-pod")
    if actor.get("latestSnapshotInfo"): reasons.append("has-snapshot")
    if actor.get("inProgressSnapshot"): reasons.append("snapshot-in-progress")
    if (actor.get("actorTemplateNamespace"), actor.get("actorTemplateName")) != (entry["template_namespace"], entry["template"]):
        reasons.append("template-mismatch")
    exists = subprocess.run([os.environ.get("KUBECTL", "kubectl"), "--context", os.environ["KUBE_CONTEXT"], "-n", entry["template_namespace"],
                             "get", "actortemplate", entry["template"], "-o", "name"], capture_output=True, text=True).returncode == 0
    if exists: reasons.append("template-still-exists")
    print(entry["id"], "KEEP" if reasons else "DELETABLE", ",".join(reasons) or "suspended,no-pod,no-snapshot,template-deleted")
PY
  cat "$evidence/classified.txt"
}

case "$action" in
  list) classify ;;
  delete)
    [[ "${RESIDUE_APPROVAL:-}" == "GOLDEN-RESIDUE:$KUBE_CONTEXT" ]] || die "set RESIDUE_APPROVAL=GOLDEN-RESIDUE:$KUBE_CONTEXT only after the owner approves"
    classify > /dev/null
    if grep -v "not-found" "$evidence/classified.txt" | grep -q " KEEP "; then
      die "refusing: a reviewed actor is not deletable ($(grep ' KEEP ' "$evidence/classified.txt" | tr '\n' ';'))"
    fi
    while read -r id verdict _; do
      [[ "$verdict" == DELETABLE ]] || continue
      "$ate" --context "$KUBE_CONTEXT" delete actor "$id" --atespace "$atespace" | tee -a "$evidence/deleted.txt"
    done < "$evidence/classified.txt"
    classify > /dev/null
    if grep -q " DELETABLE " "$evidence/classified.txt"; then die "an actor is still present after DeleteActor"; fi
    echo "golden residue removed: $(wc -l < "$evidence/deleted.txt" 2>/dev/null || echo 0) actor(s)" ;;
esac
