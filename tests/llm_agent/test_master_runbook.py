"""The master evidence runbook only cites paths, packages, tokens and flags that exist."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md"
CAMPAIGN_SCRIPT = ROOT / "scripts/llm_agent/agi_campaign.sh"


class MasterRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RUNBOOK.read_text()
        cls.script = CAMPAIGN_SCRIPT.read_text()

    def test_every_cited_repository_path_exists(self):
        paths = set(re.findall(r"(?<![\w/$.-])((?:scripts|ops|infra|configs|docs|tests)/[\w./-]+\.(?:sh|py|yaml|json|md|tmpl))", self.text))
        self.assertGreater(len(paths), 15)
        missing = sorted(p for p in paths if not (ROOT / p).exists())
        self.assertEqual(missing, [])

    def test_packages_and_approval_tokens_match_the_campaign_script(self):
        packages = set(re.search(r'PACKAGES=" ([^"]+) "', self.script).group(1).split())
        cited = set(re.findall(r"AGI_PACKAGE=([a-z0-9-]+)", self.text))
        self.assertTrue(cited and cited <= packages, cited - packages)
        self.assertEqual(cited, packages - {"agi"}, "every package has a runbook procedure")
        for token in re.findall(r"AGI_APPROVAL=([A-Z0-9-]+):\$CAMPAIGN", self.text):
            base = token.removesuffix("-ROLLBACK").lower()
            self.assertIn(base, packages, token)

    def test_watch_and_crd_tokens_match_their_scripts(self):
        watch = (ROOT / "scripts/llm_agent/sandbox_watch.sh").read_text()
        self.assertIn("package=PKG-K", watch)
        self.assertIn("package=PKG-W", watch)
        for token in re.findall(r"WATCH_APPROVAL=([A-Z-]+):\$CTX", self.text):
            self.assertRegex(token, r"^PKG-[KW](-ROLLBACK)?$")
        crd = (ROOT / "infra/kubernetes/substrate-scale/crd_patch.sh").read_text()
        self.assertIn("CRD_PATCH_GKE_APPROVED", crd)
        self.assertIn("CRD_PATCH_GKE_APPROVED=$CTX", self.text)

    def test_cloud_lab_flags_exist(self):
        cloud_lab = (ROOT / "ops/gcp/cloud_lab.py").read_text()
        for flag in re.findall(r"cloud_lab\.py [^\n]*?((?:--[a-z-]+ ?)+)", self.text):
            for name in re.findall(r"--[a-z-]+", flag):
                self.assertIn(f'"{name}"', cloud_lab, name)
        for flag in ("--scale-pdb-guarded", "--campaign", "--also-approve", "--manifest"):
            self.assertIn(flag, self.text)
        self.assertIn("CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$CAMPAIGN", self.text)

    def test_namespaces_match_the_observed_cluster(self):
        self.assertNotIn("ewallet-feature-store", self.text, "Substrate's Valkey runs in ate-system")
        self.assertIn("-n ate-system get pods", self.text)
        for namespace in ("ewallet-agents", "ewallet-mcp", "ewallet-sandbox", "ewallet-mcp-exp", "kagent", "keda"):
            self.assertIn(namespace, self.text)

    def test_as_and_bprime_are_kept_apart(self):
        self.assertIn("never reported as a KEDA-scaled WorkerPool", self.text)
        self.assertIn("It is not GKE evidence with\n  real actors", self.text)
        self.assertNotIn("--scale-pdb-guarded\n", self.text.split("## 6.")[0], "guarded sleep only in section 6")


if __name__ == "__main__":
    unittest.main()
