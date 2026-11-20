from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("feast"), "Feast test environment required")
class FeastRedisContractTest(unittest.TestCase):
    def test_pinned_feast_only_expires_keys_when_key_ttl_is_configured(self) -> None:
        import feast

        # Feature repo 0.66.0; streaming-sinks and the RAG runtime 0.67.0 (CVE-2026-18948). Both keep this TTL rule.
        self.assertIn(feast.__version__, {"0.66.0", "0.67.0"})
        provider = importlib.util.find_spec("feast.infra.online_stores.redis")
        self.assertIsNotNone(provider)
        provider_source = Path(provider.origin).read_text(encoding="utf-8")
        self.assertIn("key_ttl_seconds: Optional[int] = None", provider_source)
        self.assertIn("if online_store_config.key_ttl_seconds:", provider_source)
        self.assertIn("pipe.expire(", provider_source)


if __name__ == "__main__":
    unittest.main()
