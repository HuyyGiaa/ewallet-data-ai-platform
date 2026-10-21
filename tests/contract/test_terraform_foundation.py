from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_ROOT = REPOSITORY_ROOT / "infra" / "terraform" / "gcp"


def read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def terraform_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TERRAFORM_ROOT.rglob("*.tf"))
    )


class TerraformIgnoreContractTest(unittest.TestCase):
    def test_terraform_runtime_files_are_ignored_but_lock_file_is_tracked(self) -> None:
        gitignore = read(".gitignore")
        for pattern in (
            ".terraform/",
            "*.tfstate",
            "*.tfstate.*",
            "*.tfplan",
            ".terraform.tfstate.lock.info",
            "crash.log",
            "*.tfvars",
            "!*.tfvars.example",
        ):
            self.assertIn(pattern, gitignore)
        self.assertNotIn(".terraform.lock.hcl", gitignore)


class TerraformLayoutContractTest(unittest.TestCase):
    def test_expected_bootstrap_environment_and_modules_exist(self) -> None:
        expected_directories = {
            "bootstrap",
            "environments/dev",
            "modules/project_services",
            "modules/network",
            "modules/storage",
            "modules/iam",
            "modules/artifact_registry",
            "modules/gke",
            "modules/dataproc",
            "modules/jenkins_vm",
        }
        missing = [
            relative
            for relative in sorted(expected_directories)
            if not (TERRAFORM_ROOT / relative).is_dir()
        ]
        self.assertEqual([], missing)

    def test_each_module_has_variables_main_and_outputs(self) -> None:
        module_root = TERRAFORM_ROOT / "modules"
        missing = []
        for module in (
            "project_services",
            "network",
            "storage",
            "iam",
            "artifact_registry",
            "gke",
            "dataproc",
            "jenkins_vm",
        ):
            for filename in ("variables.tf", "main.tf", "outputs.tf"):
                path = module_root / module / filename
                if not path.is_file():
                    missing.append(path.relative_to(REPOSITORY_ROOT).as_posix())
        self.assertEqual([], missing)


class TerraformSafetyContractTest(unittest.TestCase):
    def test_environment_rejects_any_other_project_id(self) -> None:
        variables = read("infra/terraform/gcp/environments/dev/variables.tf")
        self.assertRegex(
            variables,
            r'condition\s*=\s*var\.project_id\s*==\s*"ewallet-data-ai-platform-hg26"',
        )

    def test_billable_compute_is_disabled_by_default(self) -> None:
        variables = read("infra/terraform/gcp/environments/dev/variables.tf")
        for variable_name in ("enable_gke", "enable_jenkins_vm"):
            block = re.search(
                rf'variable\s+"{variable_name}"\s*\{{(?P<body>.*?)\n\}}',
                variables,
                re.DOTALL,
            )
            self.assertIsNotNone(block, variable_name)
            self.assertRegex(block.group("body"), r"default\s*=\s*false")

    def test_canonical_and_state_buckets_prevent_destroy(self) -> None:
        bootstrap = read("infra/terraform/gcp/bootstrap/main.tf")
        storage = read("infra/terraform/gcp/modules/storage/main.tf")
        self.assertRegex(bootstrap, r"lifecycle\s*\{\s*prevent_destroy\s*=\s*true")
        self.assertRegex(storage, r"lifecycle\s*\{\s*prevent_destroy\s*=\s*true")
        self.assertIn('deletion_policy             = "PREVENT"', bootstrap)
        self.assertIn('deletion_policy             = "PREVENT"', storage)
        self.assertIn('public_access_prevention    = "enforced"', bootstrap)
        self.assertIn('public_access_prevention    = "enforced"', storage)

    def test_only_temporary_bucket_has_expiry(self) -> None:
        storage = read("infra/terraform/gcp/modules/storage/main.tf")
        variables = read("infra/terraform/gcp/modules/storage/variables.tf")
        temporary = re.search(
            r'resource\s+"google_storage_bucket"\s+"temporary"\s*\{(?P<body>.*)\n\}',
            storage,
            re.DOTALL,
        )
        self.assertIsNotNone(temporary)
        self.assertRegex(
            temporary.group("body"),
            r"age\s*=\s*var\.temporary_retention_days",
        )
        retention = re.search(
            r'variable\s+"temporary_retention_days"\s*\{(?P<body>.*?)\n\}',
            variables,
            re.DOTALL,
        )
        self.assertIsNotNone(retention)
        self.assertRegex(retention.group("body"), r"default\s*=\s*7")

        canonical = storage.split(
            'resource "google_storage_bucket" "temporary"', maxsplit=1
        )[0]
        self.assertNotRegex(canonical, r"\bage\s*=\s*7")

    def test_no_primitive_roles_keys_or_billing_resources(self) -> None:
        source = terraform_source()
        self.assertNotRegex(source, r'roles/(?:owner|editor)\b')
        self.assertNotIn('resource "google_service_account_key"', source)
        self.assertNotIn('resource "google_billing_budget"', source)

    def test_optional_compute_resources_are_count_gated(self) -> None:
        gke = read("infra/terraform/gcp/modules/gke/main.tf")
        jenkins = read("infra/terraform/gcp/modules/jenkins_vm/main.tf")
        self.assertRegex(gke, r"count\s*=\s*var\.enabled\s*\?\s*1\s*:\s*0")
        self.assertRegex(jenkins, r"count\s*=\s*var\.enabled\s*\?\s*1\s*:\s*0")

    def test_optional_compute_deletion_protection_is_explicit_and_defaults_on(self) -> None:
        environment_variables = read(
            "infra/terraform/gcp/environments/dev/variables.tf"
        )
        environment_main = read("infra/terraform/gcp/environments/dev/main.tf")
        for variable_name in (
            "gke_deletion_protection",
            "jenkins_deletion_protection",
        ):
            block = re.search(
                rf'variable\s+"{variable_name}"\s*\{{(?P<body>.*?)\n\}}',
                environment_variables,
                re.DOTALL,
            )
            self.assertIsNotNone(block, variable_name)
            self.assertRegex(block.group("body"), r"default\s*=\s*true")
            self.assertRegex(
                environment_main,
                rf"deletion_protection\s*=\s*var\.{variable_name}",
            )

    def test_gke_requires_explicit_master_authorized_cidr(self) -> None:
        gke = read("infra/terraform/gcp/modules/gke/main.tf")
        variables = read("infra/terraform/gcp/environments/dev/variables.tf")
        self.assertIn("master_authorized_networks_config", gke)
        self.assertIn("master_authorized_cidrs", gke)
        self.assertNotIn('cidr_block = "0.0.0.0/0"', gke)
        self.assertIn('variable "gke_master_authorized_cidrs"', variables)

    def test_gke_workloads_use_workload_identity_bindings(self) -> None:
        iam = read("infra/terraform/gcp/modules/iam/main.tf")
        self.assertIn(
            'resource "google_service_account_iam_member" "workload_identity"',
            iam,
        )
        self.assertIn('role               = "roles/iam.workloadIdentityUser"', iam)
        self.assertIn(".svc.id.goog[", iam)


class TerraformRunbookContractTest(unittest.TestCase):
    def test_runbook_documents_state_cost_gates_and_identity(self) -> None:
        runbook = read("docs/cloud/terraform_gcp_foundation.md")
        for required_text in (
            "Application Default Credentials",
            "terraform apply bootstrap.tfplan",
            "enable_gke        = false",
            "enable_jenkins_vm = false",
            "Dataproc",
            "terraform plan",
            "terraform destroy",
        ):
            self.assertIn(required_text, runbook)

    def test_validation_wrapper_exists_and_is_executable(self) -> None:
        wrapper = REPOSITORY_ROOT / "ops" / "validation" / "validate_terraform_foundation.sh"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(wrapper.stat().st_mode & 0o111)

    def test_docs_indexes_link_the_terraform_runbook(self) -> None:
        self.assertIn("cloud/terraform_gcp_foundation.md", read("docs/README.md"))
        self.assertIn("docs/cloud/terraform_gcp_foundation.md", read("README.md"))


if __name__ == "__main__":
    unittest.main()
