"""CI/CD V2-P3B-0: staging data seeding and staging credentials (offline; fake Redis, no cluster)."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest import mock

from ai_platform.llm_agent.tools.security import SERVICE_TOKEN_ENV, SERVICE_TOKEN_HEADER, ServiceTokenAuthenticator
from ops.llm_agent import staging_credentials as cred
from ops.llm_agent import staging_feature_seed as seed

ROOT = Path(__file__).resolve().parents[2]


class FakeRedis:
    """DUMP/RESTORE over a dict: the payload is opaque bytes, as in Redis."""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.restored = []

    def dump(self, key):
        return self.data.get(key)

    def restore(self, key, ttl, payload, replace=False):
        if key in self.data and not replace:
            raise RuntimeError("BUSYKEY")
        self.data[key] = payload
        self.restored.append(key)

    def scan_iter(self, count=100):
        return iter(list(self.data))

    def get(self, key):
        return self.data.get(key)

    def dbsize(self):
        return len(self.data)


def rag_source(keys=3):
    data = {f"rag:{i}".encode(): f"payload-{i}".encode() for i in range(keys)}
    data[seed.OWNER_KEY] = seed.RAG_PROJECT.encode()
    return FakeRedis(data)


class FeatureSeedTests(unittest.TestCase):
    def test_rag_copy_is_byte_identical_and_create_only(self):
        source, target = rag_source(), FakeRedis()
        planned = seed.copy("rag", source, target, approve=False)
        self.assertEqual((planned["written"], target.data), (False, {}))   # no write without approval
        report = seed.copy("rag", source, target, approve=True)
        self.assertEqual(report["source_digest"], report["target_digest"])
        self.assertEqual(target.data, source.data)
        self.assertEqual(seed.verify("rag", source, target)["verdict"], "PASS")

    def test_refusals_before_any_write(self):
        cases = {
            "non-empty target": (rag_source(), FakeRedis({b"other": b"x"})),
            "unbounded source": (rag_source(seed.MAX_RAG_KEYS + 1), FakeRedis()),
            "foreign owner": (FakeRedis({b"rag:0": b"x", seed.OWNER_KEY: b"ewallet_rag"}), FakeRedis()),
        }
        for name, (source, target) in cases.items():
            with self.subTest(name), self.assertRaises(seed.SeedError):
                seed.copy("rag", source, target, approve=True)
            self.assertEqual(target.restored, [])

    def test_a_missing_or_changed_key_fails_verification(self):
        source, target = rag_source(), FakeRedis()
        seed.copy("rag", source, target, approve=True)
        target.data[b"rag:0"] = b"changed"
        self.assertEqual(seed.verify("rag", source, target)["verdict"], "FAIL")
        with mock.patch.object(seed, "keys_for", return_value=[b"absent"]), self.assertRaises(seed.SeedError):
            seed.verify("fraud", FakeRedis(), FakeRedis())

    def test_only_the_feature_store_source_and_the_staging_target(self):
        good = (seed.Endpoint(seed.SOURCE_HOST, 0), seed.Endpoint(seed.TARGET_HOST, 1))
        seed.check_endpoints("fraud", *good)
        for source, target in ((seed.Endpoint(seed.SOURCE_HOST, 0), seed.Endpoint(seed.SOURCE_HOST, 1)),   # production target
                               (seed.Endpoint(seed.SOURCE_HOST, 0), seed.Endpoint(seed.TARGET_HOST, 0)),   # wrong DB
                               (seed.Endpoint(seed.TARGET_HOST, 0), seed.Endpoint(seed.TARGET_HOST, 1))):  # wrong source
            with self.subTest(source=source, target=target), self.assertRaises(seed.SeedError):
                seed.check_endpoints("fraud", source, target)

    def test_fixture_entities_are_the_four_join_keys(self):
        self.assertEqual(set(seed.fixture_entities()), set(seed.JOIN_KEYS))

    @unittest.skipUnless(importlib.util.find_spec("feast"), "feast is not installed in this environment")
    def test_fraud_keys_are_one_per_entity(self):
        keys = seed.fraud_keys(seed.fixture_entities())
        self.assertEqual(len(set(keys)), len(seed.JOIN_KEYS))


class CredentialTests(unittest.TestCase):
    def setUp(self):
        counter = iter(range(1000))
        self.documents = cred.manifests(token=lambda: f"test-token-{next(counter):04d}-" + "x" * 32)

    def test_inventory_is_ten_level_a_secrets_in_the_staging_namespaces(self):
        self.assertEqual(len(cred.SECRETS), 10)
        self.assertEqual({d["metadata"]["namespace"] for d in self.documents}, {cred.AGENTS, cred.SANDBOX})
        self.assertTrue(all(d["immutable"] and d["kind"] == "Secret" for d in self.documents))

    def test_auth_tables_parse_and_each_client_token_maps_to_its_subject(self):
        by_name = {d["metadata"]["name"]: d["stringData"] for d in self.documents}
        for server, subjects in cred.GRANTS.items():
            table = by_name[cred.auth_secret_name(server, cred.RELEASE)]["auth-config.json"]
            known = frozenset((*subjects, cred.DISCOVERY_SUBJECT))
            authenticator = ServiceTokenAuthenticator.from_env({SERVICE_TOKEN_ENV: table}, known_subjects=known)
            for subject in known:
                token = by_name[cred.token_secret_name(subject, server, cred.RELEASE)]["token"]
                principal = asyncio.run(authenticator.authenticate({SERVICE_TOKEN_HEADER: token}))
                self.assertEqual(principal.subject, subject)

    def test_redis_secrets_share_one_password_and_pin_the_databases(self):
        by_name = {d["metadata"]["name"]: d["stringData"] for d in self.documents}
        password = by_name["feast-staging-redis-auth"]["password"]
        self.assertTrue(by_name["feast-online-redis-staging-fraud"]["connection_string"].endswith(f"password={password},db=1"))
        self.assertTrue(by_name["feast-online-redis-staging-rag"]["connection_string"].endswith(f"password={password},db=4"))
        idempotency = by_name["coordinator-idempotency-redis-auth-staging"]
        self.assertIn(f">{idempotency['password']} ", idempotency["users.acl"])

    def test_names_are_the_ones_the_deployment_references(self):
        render_refs = (ROOT / "infra/helm/llm-agent-platform/values-staging.yaml").read_text() + \
            (ROOT / "infra/kubernetes/agent-staging/feast-redis.yaml").read_text() + \
            (ROOT / "jenkins/cd/agent-staging.contract.json").read_text()
        self.assertEqual([s.name for s in cred.SECRETS if s.name not in render_refs], [])

    def test_create_refuses_existing_secrets_and_never_prints_values(self):
        with mock.patch.object(cred, "existing", return_value=["ewallet-agents-staging/feast-staging-redis-auth"]):
            self.assertEqual(cred.main(["create", "--context", "ctx", "--approve"]), 2)
        out = io.StringIO()
        with mock.patch.object(cred, "existing", return_value=[]), mock.patch.object(cred, "_kubectl") as kubectl, \
                mock.patch.object(cred, "_token", return_value="secret-value-" + "y" * 40), redirect_stdout(out):
            self.assertEqual(cred.main(["create", "--context", "ctx"]), 0)   # plan only
        kubectl.assert_not_called()
        self.assertNotIn("secret-value-", out.getvalue())
        self.assertEqual(len(json.loads(out.getvalue().splitlines()[0])), 10)


if __name__ == "__main__":
    unittest.main()
