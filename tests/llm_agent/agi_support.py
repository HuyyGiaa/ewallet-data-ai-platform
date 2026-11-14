"""Shared helpers for the agent-integration (AGI) tests.

The drift artifacts come from ``ops/llm_agent/drift_artifacts.py fixture``: a
deterministic, explicitly synthetic fixture. Nothing here is project or
production data, and no test reaches a network service.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from ops.llm_agent import drift_artifacts

DRIFT_TOKEN = "drift-specialist-test-token-0123456789abcdef"
FEATURE_RAG_TOKEN = "feature-rag-specialist-test-token-0123456789"
CONTROLLER_TOKEN = "kagent-controller-list-only-token-0123456789"
VERSIONS = {
    "EWALLET_GIT_SHA": "c" * 40,
    "EWALLET_IMAGE_DIGEST": "sha256:" + "a" * 64,
    "EWALLET_CONFIG_REVISION": "agi-test",
    "EWALLET_PROMPT_REVISION": "no-prompt",
    "EWALLET_MODEL_REVISION": "fraud-detector/1",
    "EWALLET_FEATURE_REVISION": "agi-fixture-synthetic-v1",
    "EWALLET_CORPUS_REVISION": "fraud-policy-v1-20261030",
}
ACCOUNT_FEATURE = "account_behavior__account_tx_count_1h"
ACCOUNT_BASELINE = "bl-agi-fixture-v1.account_tx_count_1h"
USER_FEATURE = "user_behavior__user_avg_amount_30d"
USER_BASELINE = "bl-agi-fixture-v1.user_avg_amount_30d"
CURRENT_WINDOW = {"start": "2026-05-01T00:00:00+00:00", "end": "2026-05-31T00:00:00+00:00"}


class DriftFixture:
    """A fresh fixture directory per test class; removed by ``close``."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="agi-drift-")
        self.root = Path(self._directory.name)
        self.drift_env = drift_artifacts.write_fixture(self.root / "artifacts")

    @property
    def baselines(self) -> Path:
        return Path(self.drift_env["EWALLET_DRIFT_BASELINES_PATH"])

    @property
    def window_manifest(self) -> Path:
        return Path(self.drift_env["EWALLET_DRIFT_WINDOW_MANIFEST"])

    def server_env(self, **overrides: str) -> dict[str, str]:
        """Production drift-mcp wiring with real service-token authentication and grants."""

        env = {
            **VERSIONS,
            **self.drift_env,
            "EWALLET_MCP_TOOL_GRANTS": json.dumps({"drift-specialist": ["drift_detection"],
                                                   # Authenticated, but not granted drift_detection.
                                                   "feature-rag-specialist": ["feature_lookup"]}),
            "EWALLET_MCP_DISCOVERY_SUBJECTS": json.dumps(["kagent-controller"]),
            "EWALLET_MCP_SERVICE_TOKENS": json.dumps({"version": 1, "credentials": [
                {"id": "drift", "subject": "drift-specialist", "token": DRIFT_TOKEN, "status": "active"},
                {"id": "rag", "subject": "feature-rag-specialist", "token": FEATURE_RAG_TOKEN, "status": "active"},
                {"id": "kagent", "subject": "kagent-controller", "token": CONTROLLER_TOKEN, "status": "active"},
            ]}),
        }
        env.update(overrides)
        return env

    def close(self) -> None:
        self._directory.cleanup()
