#!/usr/bin/env bash
# PKG-R-IAM: repository-scoped read for Substrate's atelet, a separate approval from PKG-R.
#   role       roles/artifactregistry.reader
#   resource   Artifact Registry repository ewallet-platform (asia-southeast1), never the project
#   member     the Workload Identity Federation principal of KSA ate-system/atelet:
#              principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/
#              <project>.svc.id.goog/subject/ns/ate-system/sa/atelet
# No service account and no key is created. add/remove-iam-policy-binding change one member of one
# role and keep every other binding; the script proves that against a snapshot taken first.
#
# usage: substrate_pull_iam.sh <plan|grant|verify|revoke> [evidence-dir]
#   plan    [READ-ONLY]  derive the principal, read the repository policy, report present/absent
#   grant   [MUTATES]    IAM_APPROVAL=PKG-R-IAM:<project>; adds the one binding, then checks that every
#                        binding present before is still present
#   verify  [READ-ONLY]  exit 0 only when the binding is present
#   revoke  [MUTATES]    IAM_APPROVAL=PKG-R-IAM-REVOKE:<project>; removes only that member from that role
# Env: GCLOUD.
set -euo pipefail
action=${1:-} evidence=${2:-}
die() { echo "substrate_pull_iam: FAIL: $*" >&2; exit 1; }
case "$action" in plan|grant|verify|revoke) ;; *) die "usage: substrate_pull_iam.sh <plan|grant|verify|revoke> [evidence-dir]";; esac
gcloud=${GCLOUD:-gcloud}
PROJECT=ewallet-data-ai-platform-hg26 REPOSITORY=ewallet-platform REGION=asia-southeast1
CLUSTER=ewallet-dev ZONE=asia-southeast1-b ROLE=roles/artifactregistry.reader KSA_NAMESPACE=ate-system KSA=atelet
umask 077
if [[ -n "$evidence" ]]; then mkdir -p "$evidence"; else evidence=$(mktemp -d); fi

principal() {
  local number pool
  number=$("$gcloud" projects describe "$PROJECT" --format='value(projectNumber)')
  [[ "$number" =~ ^[0-9]{6,20}$ ]] || die "project number '$number' is not numeric"
  pool=$("$gcloud" container clusters describe "$CLUSTER" --location "$ZONE" --project "$PROJECT" --format='value(workloadIdentityConfig.workloadPool)')
  [[ "$pool" == "$PROJECT.svc.id.goog" ]] || die "cluster Workload Identity pool is '$pool'"
  echo "principal://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$pool/subject/ns/$KSA_NAMESPACE/sa/$KSA"
}

policy() {   # policy <file>: the repository policy as JSON
  "$gcloud" artifacts repositories get-iam-policy "$REPOSITORY" --location "$REGION" --project "$PROJECT" --format=json > "$1"
}

pairs() {    # pairs <policy file>: one "role member" line per binding member, sorted
  python3 -c 'import json,sys
for b in json.load(open(sys.argv[1])).get("bindings", []):
    for m in b.get("members", []): print(b["role"], m)' "$1" | sort
}

has_binding() { pairs "$1" | grep -qxF "$ROLE $MEMBER"; }

MEMBER=$(principal)
policy "$evidence/policy-before.json"
pairs "$evidence/policy-before.json" > "$evidence/pairs-before.txt"
echo "repository projects/$PROJECT/locations/$REGION/repositories/$REPOSITORY; role $ROLE; member $MEMBER" > "$evidence/plan.txt"

case "$action" in
  plan)
    if has_binding "$evidence/policy-before.json"; then echo "present" >> "$evidence/plan.txt"; else echo "absent" >> "$evidence/plan.txt"; fi
    cat "$evidence/plan.txt"; echo "existing bindings:"; cat "$evidence/pairs-before.txt" ;;
  verify)
    has_binding "$evidence/policy-before.json" || die "binding absent: $ROLE for $MEMBER on $REPOSITORY"
    echo "PKG-R-IAM present: $ROLE for $MEMBER on $REPOSITORY" ;;
  grant|revoke)
    token=PKG-R-IAM; [[ "$action" == revoke ]] && token=PKG-R-IAM-REVOKE
    [[ "${IAM_APPROVAL:-}" == "$token:$PROJECT" ]] || die "set IAM_APPROVAL=$token:$PROJECT only after the owner approves this exact change"
    if [[ "$action" == grant ]]; then
      has_binding "$evidence/policy-before.json" && { echo "already present; nothing to change"; exit 0; }
      "$gcloud" artifacts repositories add-iam-policy-binding "$REPOSITORY" --location "$REGION" --project "$PROJECT" \
        --member "$MEMBER" --role "$ROLE" --format=json > "$evidence/grant.json"
    else
      has_binding "$evidence/policy-before.json" || { echo "already absent; nothing to change"; exit 0; }
      "$gcloud" artifacts repositories remove-iam-policy-binding "$REPOSITORY" --location "$REGION" --project "$PROJECT" \
        --member "$MEMBER" --role "$ROLE" --format=json > "$evidence/revoke.json"
    fi
    policy "$evidence/policy-after.json"
    pairs "$evidence/policy-after.json" > "$evidence/pairs-after.txt"
    # Every other binding must be exactly as before; only our one pair may appear or disappear.
    expected=$( { grep -vxF "$ROLE $MEMBER" "$evidence/pairs-before.txt" || true
                  if [[ "$action" == grant ]]; then echo "$ROLE $MEMBER"; fi; } | sort)
    [[ "$(cat "$evidence/pairs-after.txt")" == "$expected" ]] \
      || die "repository policy after $action differs beyond the one binding (see $evidence/pairs-before.txt and pairs-after.txt)"
    echo "PKG-R-IAM $action PASS: other bindings unchanged" ;;
esac
