"""Agent integration: independent Fraud and RAG Feast targets for feature-rag-mcp.

Pure configuration tests; no Redis or registry is contacted. ``feast.FeatureStore``
is replaced by a recorder so the test can read the exact Online Store
connection given to the RAG project.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from unittest import mock

from ai_platform.inference.errors import ConfigurationError
from ai_platform.llm_agent.tools.service import (
    ACCESS_GUARANTEE,
    _rag_feature_store,
    create_feature_rag_app,
    feature_targets,
)

FEAST = importlib.util.find_spec("feast") is not None
FRAUD = "redis.feature-store.svc:6379,password=fraud-secret,db=2"
RAG = "redis.feature-store.svc:6379,password=rag-secret,db=3"
ISOLATED = {
    "EWALLET_FEATURE_TARGET_PROFILE": "isolated",
    "EWALLET_REDIS_CONNECTION_STRING": FRAUD,
    "EWALLET_FEAST_REGISTRY": "/feast/fraud/registry.db",
    "EWALLET_FRAUD_ONLINE_DB": "2",
    "EWALLET_RAG_REDIS_CONNECTION_STRING": RAG,
    "EWALLET_RAG_FEAST_REGISTRY": "/feast/rag/registry.db",
    "EWALLET_RAG_FEAST_PROJECT": "ewallet_rag_hybrid_20261114t144825",
    "EWALLET_RAG_ONLINE_DB": "3",
}


class FeatureTargetTests(unittest.TestCase):
    def test_legacy_profile_is_the_default_and_reports_the_shared_connection(self):
        self.assertEqual(feature_targets({}), {"profile": "legacy", "rag_connection": "shared-with-fraud",
                                               "access": ACCESS_GUARANTEE})
        self.assertEqual(feature_targets({"EWALLET_RAG_REDIS_CONNECTION_STRING": RAG})["rag_connection"], "dedicated")

    def test_isolated_profile_reports_targets_without_secrets(self):
        description = feature_targets(ISOLATED)
        self.assertEqual(description["fraud"]["redis_database"], 2)
        self.assertEqual(description["rag"], {"redis_database": 3, "project": "ewallet_rag_hybrid_20261114t144825",
                                              "registry": "/feast/rag/registry.db", "store": "verification"})
        self.assertNotIn("secret", repr(description))
        self.assertNotIn("feature-store.svc", repr(description))

    def test_production_fraud_store_needs_an_explicit_opt_in(self):
        production = {**ISOLATED, "EWALLET_REDIS_CONNECTION_STRING": FRAUD.replace("db=2", "db=0"),
                      "EWALLET_FRAUD_ONLINE_DB": "0"}
        for flag in (None, "false", "yes"):
            with self.subTest(flag=flag), self.assertRaises(ConfigurationError):
                feature_targets({**production, **({"EWALLET_FRAUD_ALLOW_PRODUCTION_DB": flag} if flag else {})})
        description = feature_targets({**production, "EWALLET_FRAUD_ALLOW_PRODUCTION_DB": "true"})
        self.assertEqual((description["fraud"]["redis_database"], description["fraud"]["store"]), (0, "production"))

    def test_access_claim_is_application_level_only(self):
        for values in ({}, ISOLATED):
            with self.subTest(profile=values.get("EWALLET_FEATURE_TARGET_PROFILE", "legacy")):
                description = feature_targets(values)
                self.assertEqual(description["access"]["enforced_by"], "application")
                self.assertIn("not restricted", description["access"]["redis_credential"])
                text = json.dumps(description).lower()
                self.assertNotIn("acl-enforced", text)
                self.assertNotIn('"read-only"', text)

    def test_isolated_profile_never_falls_back(self):
        cases = {
            "fraud without db (Redis default DB 0)": {"EWALLET_REDIS_CONNECTION_STRING": "redis.feature-store.svc:6379"},
            "fraud db differs from pin": {"EWALLET_FRAUD_ONLINE_DB": "4"},
            "rag connection missing": {"EWALLET_RAG_REDIS_CONNECTION_STRING": ""},
            "rag on production DB 0": {"EWALLET_RAG_REDIS_CONNECTION_STRING": RAG.replace("db=3", "db=0"),
                                       "EWALLET_RAG_ONLINE_DB": "0"},
            "rag production project": {"EWALLET_RAG_FEAST_PROJECT": "ewallet_rag"},
            "rag db differs from pin": {"EWALLET_RAG_ONLINE_DB": "5"},
            "rag registry missing": {"EWALLET_RAG_FEAST_REGISTRY": ""},
            "pin not a number": {"EWALLET_RAG_ONLINE_DB": "three"},
            "fraud pin missing": {"EWALLET_FRAUD_ONLINE_DB": ""},
            "rag pin missing": {"EWALLET_RAG_ONLINE_DB": ""},
            "fraud and rag on the same database": {"EWALLET_REDIS_CONNECTION_STRING": FRAUD.replace("db=2", "db=3"),
                                                   "EWALLET_FRAUD_ONLINE_DB": "3"},
            "unknown profile": {"EWALLET_FEATURE_TARGET_PROFILE": "shared"},
        }
        for label, change in cases.items():
            with self.subTest(label), self.assertRaises(ConfigurationError):
                feature_targets({**ISOLATED, **change})

    def test_misconfigured_isolated_app_stops_before_opening_any_store(self):
        with mock.patch("ai_platform.inference.config.InferenceSettings.from_env") as settings:
            with self.assertRaises(ConfigurationError):
                create_feature_rag_app({**ISOLATED, "EWALLET_RAG_ONLINE_DB": "9"})
            settings.assert_not_called()


class _Recorder:
    created: list = []

    def __init__(self, *, config=None, repo_path=None):
        self.config = config
        _Recorder.created.append(self)


class _OnlineStore:
    type = "redis"

    def __init__(self, connection_string):
        self.connection_string = connection_string


class _Fraud:
    def __init__(self, connection_string):
        self.config = type("Config", (), {"online_store": _OnlineStore(connection_string)})()


@unittest.skipUnless(FEAST, "Feast unavailable: uv sync --frozen --extra feast in infra/docker/llm-agent-tooling")
class RagStoreConnectionTests(unittest.TestCase):
    def setUp(self):
        _Recorder.created = []

    def rag_store(self, values):
        with mock.patch("feast.FeatureStore", _Recorder):
            return _rag_feature_store(_Fraud(FRAUD), values, timeout_seconds=2.0)

    def test_dedicated_rag_connection_is_used_instead_of_the_fraud_one(self):
        store = self.rag_store(ISOLATED)
        self.assertTrue(store.config.online_store.connection_string.startswith(RAG))
        self.assertEqual(store.config.project, "ewallet_rag_hybrid_20261114t144825")

    def test_legacy_rag_project_reuses_the_fraud_connection(self):
        store = self.rag_store({"EWALLET_RAG_FEAST_REGISTRY": "/feast/rag/registry.db"})
        self.assertTrue(store.config.online_store.connection_string.startswith(FRAUD))
        self.assertEqual(store.config.project, "ewallet_rag")

    def test_dedicated_connection_without_registry_is_refused(self):
        fraud = _Fraud(FRAUD)
        with self.assertRaises(ConfigurationError):
            _rag_feature_store(fraud, {"EWALLET_RAG_REDIS_CONNECTION_STRING": RAG}, timeout_seconds=2.0)

    def test_without_rag_registry_the_fraud_store_is_returned_unchanged(self):
        fraud = _Fraud(FRAUD)
        self.assertIs(_rag_feature_store(fraud, {}, timeout_seconds=2.0), fraud)


if __name__ == "__main__":
    unittest.main()
