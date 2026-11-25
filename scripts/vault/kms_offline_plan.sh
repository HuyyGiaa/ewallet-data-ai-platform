#!/usr/bin/env bash
# OFFLINE ONLY: a real `terraform plan` of the dev root, targeted at module.vault_kms, against an EMPTY LOCAL
# state in a scratch copy. It never uses the GCS backend, the real state or a credential:
#   - the scratch copy gets backend_override.tf (local backend, empty state) and offline_override.tf
#     (a placeholder access token, so the provider never looks for Application Default Credentials);
#   - `terraform plan` runs with every HTTP(S) request sent to a dead local proxy (127.0.0.1:9), so no request
#     can reach Google; a plan that needed the network fails here instead;
#   - the JSON is written with the placeholder token removed.
# It proves the module's dependency closure (only its own resources are planned) and gives the guard tests a
# plan in Terraform's real format. It does NOT show what the real dev state would plan: that is P4-B.
#
# usage: kms_offline_plan.sh <out-dir> [provider-mirror-dir]
#   provider-mirror-dir: a filesystem mirror holding registry.terraform.io/hashicorp/google/<version>/linux_amd64;
#   without it, `terraform init` downloads the provider from the Terraform registry (no GCP access).
set -euo pipefail
out=${1:?out-dir} mirror=${2:-}
root=$(cd "$(dirname "$0")/../.." && pwd)
out=$(realpath -m "$out"); case "$out/" in "$root"/*) echo "REFUSED: out-dir must be outside the repository" >&2; exit 2 ;; esac
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$out"
cp -r "$root/infra/terraform/gcp" "$work/"
find "$work" -name .terraform -type d -prune -exec rm -rf {} +
dev=$work/gcp/environments/dev
printf 'terraform {\n  backend "local" {\n    path = "%s/empty.tfstate"\n  }\n}\n' "$work" > "$dev/backend_override.tf"
printf 'provider "google" {\n  access_token = "offline-placeholder-not-a-credential"\n}\n' > "$dev/offline_override.tf"
export TF_IN_AUTOMATION=1 TF_INPUT=0
unset GOOGLE_APPLICATION_CREDENTIALS GOOGLE_CREDENTIALS GOOGLE_OAUTH_ACCESS_TOKEN CLOUDSDK_CONFIG
if [ -n "$mirror" ]; then
  printf 'provider_installation {\n  filesystem_mirror {\n    path    = "%s"\n    include = ["registry.terraform.io/hashicorp/google"]\n  }\n  direct { exclude = ["registry.terraform.io/hashicorp/google"] }\n}\n' "$(realpath "$mirror")" > "$work/cli.tfrc"
  export TF_CLI_CONFIG_FILE=$work/cli.tfrc
fi
terraform -chdir="$dev" init -input=false -no-color -reconfigure > "$out/init.log"
blackhole=(env HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 NO_PROXY= no_proxy=)
"${blackhole[@]}" terraform -chdir="$dev" plan -input=false -no-color -lock=false -target=module.vault_kms \
  -var enable_vault_kms=true -var project_id=ewallet-data-ai-platform-hg26 -out="$work/p4.tfplan" > "$out/plan.log"
"${blackhole[@]}" terraform -chdir="$dev" show -json "$work/p4.tfplan" | python3 -c '
import json, sys
plan = json.load(sys.stdin)
plan["configuration"]["provider_config"]["google"]["expressions"].pop("access_token", None)
json.dump(plan, open(sys.argv[1], "w"), indent=1, sort_keys=True)' "$out/targeted.plan.json"
grep -q "offline-placeholder" "$out/targeted.plan.json" && { echo "REFUSED: placeholder token left in the plan" >&2; exit 1; }
grep -E '^Plan:' "$out/plan.log"
echo "OFFLINE_TARGETED_PLAN_WRITTEN $out/targeted.plan.json (LOCAL EMPTY STATE: not the dev environment)"
