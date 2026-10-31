#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
terraform_root="${repository_root}/infra/terraform/gcp"
python_bin="${PYTHON_BIN:-python3}"

terraform fmt -recursive -check "${terraform_root}"

terraform -chdir="${terraform_root}/bootstrap" init -backend=false -input=false
terraform -chdir="${terraform_root}/bootstrap" validate

terraform -chdir="${terraform_root}/environments/dev" init -backend=false -input=false
terraform -chdir="${terraform_root}/environments/dev" validate

cd "${repository_root}"
"${python_bin}" -m unittest tests.contract.test_terraform_foundation -v

echo "TERRAFORM FOUNDATION VALIDATION PASS"
