from __future__ import annotations

from pathlib import Path
import re
import unittest

from scripts.ci.catalog import load_catalog


ROOT = Path(__file__).resolve().parents[2]


class JenkinsEntrypointTests(unittest.TestCase):
    def test_both_entrypoints_are_thin_wrappers_over_one_helper(self) -> None:
        expectations = {
            "Jenkinsfile": "platform",
            "Jenkinsfile.llm-agent": "llm-agent",
        }
        for relative, profile in expectations.items():
            with self.subTest(relative=relative):
                lines = [line for line in (ROOT / relative).read_text().splitlines() if line.strip()]
                text = "\n".join(lines)
                self.assertLessEqual(len(lines), 8)
                self.assertIn("checkout scm", text)
                self.assertIn("load 'jenkins/pipeline/componentPipeline.groovy'", text)
                self.assertIn(f"profile: '{profile}'", text)
                self.assertIn("componentPipeline.run", text)

    def test_catalog_owns_the_llm_compatibility_profile(self) -> None:
        catalog = load_catalog(ROOT / "jenkins/catalog/components.json")
        self.assertEqual(
            catalog.profiles["llm-agent"],
            ("llm-serving", "rag-pipeline", "mcp-agents", "llm-d-routing"),
        )


class JenkinsSharedPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (ROOT / "jenkins/pipeline/componentPipeline.groovy").read_text()

    def test_deploy_is_disabled_by_default_and_exact_sha_is_required(self) -> None:
        self.assertRegex(self.text, r"booleanParam\(name: 'DEPLOY_APPROVED', defaultValue: false")
        self.assertIn('test -n "$EXPECTED_GIT_SHA"', self.text)
        self.assertIn('test "\\$(git rev-parse HEAD)" = "$EXPECTED_GIT_SHA"', self.text)

    def test_uses_merge_base_detector_and_shared_runner(self) -> None:
        self.assertIn("python3 -m scripts.ci.change_detection", self.text)
        self.assertIn("python3 -m scripts.ci.run_ci", self.text)
        self.assertNotRegex(self.text, re.compile(r"HEAD\^|HEAD~"))
        self.assertNotIn("ai_platform/", self.text)
        self.assertNotIn("data_platform/", self.text)

    def test_shells_are_strict_and_evidence_archive_cannot_be_empty(self) -> None:
        self.assertGreaterEqual(self.text.count("set -euo pipefail"), 3)
        self.assertRegex(self.text, r"archiveArtifacts[^\n]+allowEmptyArchive: false")
        self.assertIn("junit allowEmptyResults: true", self.text)

    def test_pipeline_records_missing_jenkins_runtime_as_unverified(self) -> None:
        self.assertIn("jenkins_runtime_verified", self.text)
        self.assertNotIn('"jenkins_runtime_verified": true', self.text)


if __name__ == "__main__":
    unittest.main()
