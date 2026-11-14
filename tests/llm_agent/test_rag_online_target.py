"""The RAG Redis target is explicit, isolated from production and exclusively owned."""

from __future__ import annotations

import json
import unittest

from ai_platform.features.feast import redis_isolation
from ai_platform.llm_agent.rag import online_target as ot


class FakeRedis:
    """Implements the claim script's semantics (GET marker, DBSIZE, SET) without Lua."""

    def __init__(self, keys=None, run_id="run-a"):
        self.keys = dict(keys or {})
        self.run_id = run_id

    def info(self, section):
        assert section == "server"
        return {"run_id": self.run_id}

    def get(self, key):
        return self.keys.get(key)

    def dbsize(self):
        return len(self.keys)

    def eval(self, _script, _numkeys, key, value):
        current = self.keys.get(key)
        if current is not None:
            return "OWNED" if current == value else "CONFLICT"
        if self.keys:
            return "AMBIGUOUS_NONEMPTY"
        self.keys[key] = value
        return "CLAIMED"


IDENTITY = {"registry": "/r/registry.db", "corpus_revision": "c1", "feature_index_revision": "f1",
            "manifest_sha256": "a" * 64}


def target(db=3, project="ewallet_rag_verify", mode=None):
    environment = {"EWALLET_RAG_ONLINE_TARGET": mode} if mode else {}
    return ot.resolve(f"127.0.0.1:6379,password=p,db={db}", project=project, environment=environment)


class ParseTests(unittest.TestCase):
    def test_explicit_database_is_required_in_both_syntaxes(self):
        for value in ("h:6379", "h:6379,password=x", "redis://h:6379", "redis://:x@h:6379/"):
            with self.subTest(value=value), self.assertRaisesRegex(ot.RagTargetError, "explicitly"):
                ot.parse_connection(value)
        self.assertEqual(ot.parse_connection("h:6379,password=x,db=4").database, 4)
        endpoint = ot.parse_connection("rediss://:p%40ss@h:6380/5")
        self.assertEqual((endpoint.host, endpoint.port, endpoint.database, endpoint.password, endpoint.ssl),
                         ("h", 6380, 5, "p@ss", True))

    def test_password_never_appears_in_repr_or_description(self):
        resolved = target()
        self.assertNotIn("password=p", repr(resolved.endpoint))
        self.assertNotIn("'p'", repr(resolved.endpoint))
        self.assertEqual(resolved.describe(), {"mode": "verification", "redis_database": 3,
                                               "project": "ewallet_rag_verify"})


class ModeTests(unittest.TestCase):
    def test_verification_refuses_production_and_canonical_verify_databases(self):
        for db in (0, 1):
            with self.subTest(db=db), self.assertRaisesRegex(ot.RagTargetError, "2-15"):
                target(db=db)
        for db in (2, 15):
            self.assertEqual(target(db=db).endpoint.database, db)

    def test_verification_needs_a_dedicated_project(self):
        for project in ("ewallet_rag", "other", "ewallet_ragx"):
            with self.subTest(project=project), self.assertRaisesRegex(ot.RagTargetError, "own Feast project"):
                target(project=project)

    def test_production_is_db0_and_ewallet_rag_only_and_must_be_explicit(self):
        self.assertEqual(target(db=0, project="ewallet_rag", mode="production").mode, "production")
        for db, project in ((3, "ewallet_rag"), (0, "ewallet_rag_verify")):
            with self.subTest(db=db, project=project), self.assertRaises(ot.RagTargetError):
                target(db=db, project=project, mode="production")
        with self.assertRaisesRegex(ot.RagTargetError, "must be one of"):
            target(mode="staging")


class OwnershipTests(unittest.TestCase):
    def test_empty_database_is_claimed_then_owned_then_verified(self):
        client = FakeRedis()
        first = ot.ensure_ownership(target(), client, **IDENTITY)
        self.assertEqual(first["claim_status"], "CLAIMED")
        self.assertTrue(first["target_fingerprint"].startswith("sha256:"))
        self.assertEqual(ot.ensure_ownership(target(), client, **IDENTITY)["claim_status"], "OWNED")
        self.assertEqual(ot.verify_ownership(target(), client, **IDENTITY)["owner"], "MATCH")
        marker = json.loads(client.keys[redis_isolation.OWNER_KEY])
        self.assertEqual((marker["campaign_id"], marker["online_store_id"]), ("ewallet_rag_verify", ot.ONLINE_STORE_ID))

    def test_nonempty_or_foreign_database_stops_the_run_without_writing(self):
        busy = FakeRedis({"some-feature-key": b"x"})
        with self.assertRaisesRegex(ot.RagTargetError, "AMBIGUOUS_NONEMPTY"):
            ot.ensure_ownership(target(), busy, **IDENTITY)
        self.assertEqual(set(busy.keys), {"some-feature-key"})
        owned = FakeRedis()
        ot.ensure_ownership(target(project="ewallet_rag_other"), owned, **IDENTITY)
        with self.assertRaisesRegex(ot.RagTargetError, "CONFLICT"):
            ot.ensure_ownership(target(), owned, **IDENTITY)

    def test_restart_or_changed_identity_fails_verification(self):
        client = FakeRedis()
        ot.ensure_ownership(target(), client, **IDENTITY)
        client.run_id = "run-b"  # Redis restarted: the fingerprint no longer matches
        with self.assertRaisesRegex(ot.RagTargetError, "no longer owned"):
            ot.verify_ownership(target(), client, **IDENTITY)

    def test_production_is_never_claimed(self):
        client = FakeRedis({"production": b"x"})
        result = ot.ensure_ownership(target(db=0, project="ewallet_rag", mode="production"), client, **IDENTITY)
        self.assertEqual(result["claim_status"], "NOT_CLAIMED_PRODUCTION")
        self.assertEqual(set(client.keys), {"production"})


class PreviewTests(unittest.TestCase):
    """The preflight forecast agrees with the claim and never writes."""

    def test_preview_matches_the_claim_outcome_without_writing(self):
        client = FakeRedis()
        self.assertEqual(ot.preview_ownership(target(), client, **IDENTITY)["ownership"], "CLAIMABLE_EMPTY")
        self.assertEqual(client.keys, {})
        ot.ensure_ownership(target(), client, **IDENTITY)
        self.assertEqual(ot.preview_ownership(target(), client, **IDENTITY)["ownership"], "OWNED_MATCH")
        before = dict(client.keys)
        client.run_id = "run-b"  # restarted Redis (e.g. after a cluster sleep and wake)
        restarted = ot.preview_ownership(target(), client, **IDENTITY)
        self.assertEqual((restarted["ownership"], restarted["differing_fields"]), ("CONFLICT", ["target_fingerprint"]))
        other_registry = ot.preview_ownership(target(), FakeRedis(before), **{**IDENTITY, "registry": "/other.db"})
        self.assertEqual(other_registry["differing_fields"], ["registry_uri"])
        self.assertEqual(client.keys, before)

    def test_unowned_keys_are_reported_as_another_workload(self):
        busy = FakeRedis({"feature-row": b"x"})
        self.assertEqual(ot.preview_ownership(target(), busy, **IDENTITY)["ownership"], "NONEMPTY_UNOWNED")
        self.assertEqual(set(busy.keys), {"feature-row"})


class StoragePathTests(unittest.TestCase):
    def test_gcs_paths_of_a_verification_run_carry_its_project(self):
        verify = target()
        ot.check_storage_paths(verify, registry="gs://b/features/registries/rag/ewallet_rag_verify/registry.db",
                               publication_base="gs://b/features/rag/ewallet_rag_verify")
        production_paths = ("gs://b/features/registries/rag/fraud-policy-feast-v1/registry.db", "gs://b/features/rag")
        for registry, base in ((production_paths[0], "gs://b/features/rag/ewallet_rag_verify"),
                               ("gs://b/features/registries/rag/ewallet_rag_verify/registry.db", production_paths[1])):
            with self.subTest(registry=registry, base=base), self.assertRaisesRegex(ot.RagTargetError, "segment"):
                ot.check_storage_paths(verify, registry=registry, publication_base=base)
        ot.check_storage_paths(verify, registry="/home/u/feast/registry.db", publication_base="/work/feast")
        ot.check_storage_paths(target(db=0, project="ewallet_rag", mode="production"),
                               registry=production_paths[0], publication_base=production_paths[1])


if __name__ == "__main__":
    unittest.main()
