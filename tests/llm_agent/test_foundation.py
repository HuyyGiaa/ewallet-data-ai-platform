from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_platform.llm_agent.config import ARCHITECTURE_CONTRACT, load_release_config
from ops.llm_agent.changed_components import detect
from ops.llm_agent.preflight import validate_source


ROOT = Path(__file__).resolve().parents[2]


class FoundationTest(unittest.TestCase):
    def test_release_inventory_is_complete_and_never_runtime_verified(self) -> None:
        config = load_release_config(
            ROOT / "configs/llm_agent/release-v1.json", repository_root=ROOT
        )
        self.assertEqual(config.architecture, ARCHITECTURE_CONTRACT)
        self.assertEqual(len(config.components), 12)
        self.assertNotIn("VERIFIED", {item.runtime_status for item in config.components.values()})

    def test_conflicting_architecture_fails_closed(self) -> None:
        original = json.loads((ROOT / "configs/llm_agent/release-v1.json").read_text())
        original["architecture"]["external_gateway"] = "nginx"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.json"
            path.write_text(json.dumps(original))
            with self.assertRaisesRegex(ValueError, "Living Decision 21.216"):
                load_release_config(path, repository_root=ROOT)

    def test_changed_component_detection_is_deterministic(self) -> None:
        changed = detect(
            [
                "data_platform/processing/flink/job.py",
                "ai_platform/llm_agent/contracts.py",
                "README.md",
            ]
        )
        self.assertEqual(
            changed,
            [
                "drift-mcp",
                "feature-rag-mcp",
                "mcp-gateway",
                "streaming-to-offline",
                "streaming-to-online",
            ],
        )

    def test_gate_a_preflight_renders_both_overlays(self) -> None:
        result = validate_source(ROOT)
        self.assertEqual(result["status"], "SOURCE_ONLY_PASS")
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(result["rendered_environments"], ["dev", "gcp"])


if __name__ == "__main__":
    unittest.main()
