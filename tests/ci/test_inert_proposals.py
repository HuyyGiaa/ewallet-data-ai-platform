"""Unapproved deployment changes stay inert in the canonical source.

The canonical integration keeps reviewed-but-unapproved manifests (D1, the
cluster-scoped record guard, M2, the superseded Batch D draft) byte-preserved,
and r33 (registry-sourced Drift prompt) exists only as an explicit --set override
so their review history survives. They must never be reachable from an
automated deploy path, and the Terraform CD identities stay opt-in until the
live resources are imported.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]

PROPOSALS = (
    "infra/kubernetes/jenkins/rbac/batch-d1-step1.yaml",
    "infra/kubernetes/jenkins/rbac/batch-d1-step1-r2.yaml",
    "infra/kubernetes/jenkins/rbac/batch-d1-proposal.yaml",
    "infra/kubernetes/jenkins/rbac/batch-d2-record-guard-proposal.yaml",
    "infra/kubernetes/jenkins/rbac/batch-d-production-proposal.yaml",
    "infra/kubernetes/prompt-registry/hardening-m1-m2-proposal.yaml",
)
# Files that may legitimately name a proposal: reviews, tests and evidence.
REFERENCE_ALLOWED = re.compile(r"^(docs/|tests/)")
EXECUTABLE_SUFFIXES = (".sh", ".py", ".groovy", ".json", ".yaml", ".yml", ".tf", ".toml")


def tracked_files() -> list[str]:
    output = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return output.split()


class InertProposalTests(unittest.TestCase):
    def test_every_proposal_is_present_and_labelled(self) -> None:
        for path in PROPOSALS:
            with self.subTest(path=path):
                header = "\n".join((ROOT / path).read_text(encoding="utf-8").splitlines()[:3])
                self.assertIn("PROPOSAL", header)

    def test_no_deploy_path_references_a_proposal(self) -> None:
        names = {Path(path).name for path in PROPOSALS}
        offenders = []
        for path in tracked_files():
            if REFERENCE_ALLOWED.match(path) or path in PROPOSALS or not path.endswith(EXECUTABLE_SUFFIXES):
                continue
            file = ROOT / path
            if not file.is_file():
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            offenders += [f"{path} -> {name}" for name in names if name in text]
        self.assertEqual(offenders, [])

    def test_catalog_deploy_targets_exclude_proposals(self) -> None:
        catalog = json.loads((ROOT / "jenkins/catalog/components.json").read_text(encoding="utf-8"))
        for name, component in catalog["components"].items():
            for field in ("deploy_handler", "deployment_target"):
                value = component.get(field) or ""
                with self.subTest(component=name, field=field):
                    self.assertFalse(any(value and value in proposal for proposal in PROPOSALS))
                    self.assertNotIn("proposal", value)

    def test_kustomizations_do_not_include_proposals(self) -> None:
        for kustomization in ROOT.glob("infra/**/kustomization.yaml"):
            text = kustomization.read_text(encoding="utf-8")
            with self.subTest(path=str(kustomization.relative_to(ROOT))):
                self.assertNotIn("proposal", text.lower())

    def test_no_committed_values_activate_the_registry_prompt_source(self) -> None:
        # r33 (Drift Specialist prompt from agentregistry) is an explicit
        # --set override only; no committed values file may enable it.
        offenders = []
        for path in tracked_files():
            if REFERENCE_ALLOWED.match(path) or not path.endswith((".yaml", ".yml")):
                continue
            text = (ROOT / path).read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*promptSource:\s*[\"']?registry", text, re.MULTILINE):
                offenders.append(path)
        self.assertEqual(offenders, [])

    def test_terraform_in_cluster_cd_identities_stay_opt_in(self) -> None:
        variables = (ROOT / "infra/terraform/gcp/environments/dev/variables.tf").read_text(encoding="utf-8")
        block = variables[variables.index('variable "enable_in_cluster_cd"'):]
        block = block[: block.index("}") + 1]
        self.assertRegex(block, r"default\s*=\s*false")


if __name__ == "__main__":
    unittest.main()
