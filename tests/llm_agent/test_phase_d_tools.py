"""Phase D tool services: contracts, failure handling and the FastAPI/MCP boundary.

Every backend below is a TEST FIXTURE injected through the tool protocols.
Passing proves source behaviour only: no Feast Online, KServe, vector store,
drift baseline or deployed service was contacted.

Run with an interpreter that has FastAPI and Pydantic (the ``ewallet-ml``
conda environment); the module skips elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import logging
import math
from pathlib import Path
import random
import unittest

FASTAPI = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("pydantic") is not None

if FASTAPI:
    from ai_platform.features.feast.schema import FEATURE_SPECS
    from ai_platform.inference.assembler import HistoricalFeatureSet
    from ai_platform.inference.errors import (
        ConfigurationError,
        DependencyNotReadyError,
        FeatureBackendError,
        FeatureContractError,
        FeatureTimeoutError,
        MissingFeatureError,
        ModelSignatureError,
        ModelTimeoutError,
        StaleFeatureError,
    )
    from ai_platform.inference.schemas import FraudPredictionResponse
    from ai_platform.llm_agent.contracts import RequestContext, VersionContext
    from ai_platform.llm_agent.rag.contracts import Citation as RagCitation
    from ai_platform.llm_agent.rag.pipeline import (
        TrustedChunkIndex,
        chunk_documents,
        citations,
        embed_chunks,
        ingest_documents,
        retrieve_exact,
    )
    from ai_platform.llm_agent.tools.drift import (
        DriftBaseline,
        DriftDetectionTool,
        UnavailableBaselineStore,
        UnavailableWindowSampler,
        WindowSample,
        population_stability_index,
    )
    from ai_platform.llm_agent.tools.feature_lookup import FeatureLookupTool
    from ai_platform.llm_agent.tools.fraud_scoring import FraudScoringTool
    from ai_platform.llm_agent.tools.knowledge_search import (
        KnowledgeSearchTool,
        UnavailableKnowledgeRetriever,
    )
    from ai_platform.llm_agent.tools.runtime import ToolExecutor, ToolOutcome
    from ai_platform.llm_agent.tools.security import AuthenticationError, Principal, ToolGrants
    from ai_platform.llm_agent.tools.service import create_drift_app, create_tool_app, versions_from_env
    from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data/llm_agent/rag/fraud-policy-v1/manifest.json"
EVENT_TIME = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SUBJECT = "spiffe-test/feature-rag-specialist"
ACTOR = {"authorization": "Bearer fixture-token-specialist"}
ENV = {
    "EWALLET_GIT_SHA": "a" * 40,
    "EWALLET_IMAGE_DIGEST": "sha256:" + "b" * 64,
    "EWALLET_CONFIG_REVISION": "config-test-1",
    "EWALLET_PROMPT_REVISION": "no-prompt",
    "EWALLET_MODEL_REVISION": "fixture-model/1",
    "EWALLET_FEATURE_REVISION": "fixture-features-1",
    "EWALLET_CORPUS_REVISION": "fixture-corpus-1",
}


def transaction(**overrides):
    payload = {
        "transaction_id": "tx-fixture-001", "event_timestamp": EVENT_TIME.isoformat(),
        "user_id": "user-1", "account_id": "account-1", "device_id": "device-1",
        "merchant_id": "merchant-1", "type": "payment", "amount": 125000.0,
        "currency": "VND", "channel": "app",
    }
    payload.update(overrides)
    return payload


def context(timeout_ms=5000, request_id="req-1"):
    return RequestContext(request_id, "00-" + "1" * 32 + "-" + "2" * 16 + "-01", "actor-test", timeout_ms, 1)


def run(coroutine):
    return asyncio.run(coroutine)


class FixtureFeatureProvider:
    """TEST FIXTURE: deterministic historical features, no Feast access."""

    def __init__(self, error=None, delay=0.0, ready=True, value_override=None):
        self.error, self.delay, self.ready_value = error, delay, ready
        self.value_override = value_override or {}
        self.in_flight = self.peak = 0

    async def get_features(self, request):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            integers = {f"{view}__{name}" for view, spec in FEATURE_SPECS.items()
                        for name in spec.get("integer_features", ())}
            values, stamps = {}, {}
            for position, name in enumerate(HISTORICAL_FEATURE_COLUMNS):
                merchant = name.startswith("merchant_behavior__")
                if merchant and request.merchant_id is None:
                    values[name], stamps[name] = None, None
                    continue
                values[name] = position if name in integers else position + 0.5
                stamps[name] = request.event_timestamp - timedelta(minutes=1)
            values.update(self.value_override)
            return HistoricalFeatureSet(values=values, timestamps=stamps)
        finally:
            self.in_flight -= 1

    async def ready(self):
        if isinstance(self.ready_value, Exception):
            raise self.ready_value
        return self.ready_value


class FixturePredictor:
    """TEST FIXTURE: echoes a fixed prediction; not a model."""

    def __init__(self, error=None, model=("fraud-xgb", "7"), transaction_id=None, ready=True):
        self.error, self.model, self.transaction_id, self.ready_value = error, model, transaction_id, ready

    async def predict(self, request, *, correlation_id):
        if self.error:
            raise self.error
        return FraudPredictionResponse(
            transaction_id=self.transaction_id or request.transaction_id, fraud_probability=0.25,
            fraud_prediction=0, model_name=self.model[0], model_version=self.model[1],
            feature_timestamp=request.event_timestamp - timedelta(minutes=1), correlation_id=correlation_id,
        )

    async def ready(self):
        return self.ready_value


class FixtureRetriever:
    """TEST FIXTURE: exact search over the 2-document corpus with toy embeddings."""

    backend_id = "test-fixture-exact"

    def __init__(self, tamper=None):
        self.index = TrustedChunkIndex.from_manifest(MANIFEST)
        manifest, documents = ingest_documents(MANIFEST)
        chunks = chunk_documents(documents, size_words=manifest.chunk_size_words,
                                 overlap_words=manifest.chunk_overlap_words)
        vectors = {doc: (1.0, float(i)) for i, doc in enumerate(sorted({c.document_id for c in chunks}))}
        self.embedded = embed_chunks(
            chunks, embed=lambda texts: [vectors[c.document_id] for c in chunks], dimensions=2,
            model_revision=manifest.embedding_model_revision, index_revision=manifest.feature_index_revision,
            published_at=EVENT_TIME,
        )
        self.tamper = tamper

    async def search(self, query, *, top_k, min_score):
        hits = citations(retrieve_exact((1.0, 0.0), self.embedded, top_k=top_k, threshold=min_score))
        return self.tamper(hits) if self.tamper else hits

    async def ready(self):
        return None


class FixtureBaselines:
    """TEST FIXTURE baseline registry."""

    def __init__(self, baseline=None):
        self.baseline = baseline

    async def get(self, baseline_id, feature_name):
        return self.baseline if self.baseline and self.baseline.baseline_id == baseline_id else None

    async def ready(self):
        return None


class FixtureSampler:
    """TEST FIXTURE window sampler."""

    def __init__(self, values, revision="fixture-window-1", identity=None):
        self.values, self.revision, self.identity = tuple(values), revision, identity

    async def sample(self, feature_name, start, end):
        name, s, e = self.identity or (feature_name, start, end)
        return WindowSample(name, s, e, self.values, self.revision)

    async def ready(self):
        return None


def baseline(thresholds=(0.1, 0.25), min_count=10, **overrides):
    values = dict(
        baseline_id="baseline-amount-v1", feature_name="amount", method="psi",
        cut_points=(10.0, 20.0, 30.0), reference_proportions=(0.25, 0.25, 0.25, 0.25),
        smoothing=1e-4, min_current_count=min_count,
        reference_start=EVENT_TIME - timedelta(days=60), reference_end=EVENT_TIME - timedelta(days=30),
        dataset_revision="fixture-reference-1", thresholds=thresholds,
    )
    values.update(overrides)
    return DriftBaseline(**values)


def drift_args(**overrides):
    payload = {"feature_name": "amount", "baseline_id": "baseline-amount-v1",
               "current_window": {"start": (EVENT_TIME - timedelta(days=7)).isoformat(),
                                  "end": EVENT_TIME.isoformat()}}
    payload.update(overrides)
    return payload


def executor(*handlers, **kwargs):
    return ToolExecutor(handlers, versions=versions_from_env(ENV), **kwargs)


def feature_rag(provider=None, predictor=None, retriever=None, **kwargs):
    return executor(
        FeatureLookupTool(provider or FixtureFeatureProvider()),
        FraudScoringTool(predictor or FixturePredictor(), model_name="fraud-xgb", model_version="7"),
        KnowledgeSearchTool(retriever or FixtureRetriever()),
        **kwargs,
    )


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the ewallet-ml interpreter")
class ExecutorTests(unittest.TestCase):
    def test_construction_bounds(self):
        tool = FeatureLookupTool(FixtureFeatureProvider())
        for kwargs in ({"max_concurrency": 0}, {"max_concurrency": 65}, {"max_timeout_ms": 99}, {"max_timeout_ms": 120_001}):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                executor(tool, **kwargs)
        with self.assertRaises(ValueError):
            executor(tool, tool)

    def test_unknown_and_unserved_tools(self):
        tools = executor(FeatureLookupTool(FixtureFeatureProvider()))
        with self.assertRaises(ValueError):
            run(tools.call("shell", {}, context()))
        response = run(tools.call("drift_detection", {}, context()))
        self.assertEqual((response.status, response.result["error"]["code"]), ("rejected", "tool_not_served"))

    def test_invalid_arguments_are_rejected_without_echoing_values(self):
        tools = feature_rag()
        response = run(tools.call("feature_lookup", {"transaction": transaction(amount=-5, user_id="SECRET-USER")}, context()))
        self.assertEqual(response.status, "rejected")
        self.assertNotIn("SECRET-USER", json.dumps(response.to_dict()))
        self.assertEqual(run(tools.call("feature_lookup", ["not", "a", "mapping"], context())).status, "rejected")

    def test_deadline_is_enforced(self):
        tools = feature_rag(provider=FixtureFeatureProvider(delay=1.0))
        response = run(tools.call("feature_lookup", {"transaction": transaction()}, context(timeout_ms=100)))
        self.assertEqual((response.status, response.result["error"]["code"]), ("timeout", "tool_timeout"))

    def test_concurrency_is_bounded(self):
        provider = FixtureFeatureProvider(delay=0.02)
        tools = feature_rag(provider=provider, max_concurrency=2)

        async def burst():
            return await asyncio.gather(*(
                tools.call("feature_lookup", {"transaction": transaction()}, context(request_id=f"r{i}"))
                for i in range(6)))

        responses = run(burst())
        self.assertTrue(all(r.status == "ok" for r in responses))
        self.assertEqual(provider.peak, 2)
        self.assertEqual(sorted(r.request_id for r in responses), [f"r{i}" for i in range(6)])

    def test_backend_failures_map_to_shared_statuses(self):
        cases = {
            "backend": (FeatureBackendError(), "unavailable"),
            "missing": (MissingFeatureError(["x"]), "unavailable"),
            "stale": (StaleFeatureError(["x"]), "unavailable"),
            "feature timeout": (FeatureTimeoutError(), "timeout"),
            "contract": (FeatureContractError(), "error"),
            "not ready": (DependencyNotReadyError(), "unavailable"),
            "unexpected": (RuntimeError("SECRET stack detail"), "error"),
        }
        for label, (error, status) in cases.items():
            with self.subTest(label):
                response = run(feature_rag(provider=FixtureFeatureProvider(error=error)).call(
                    "feature_lookup", {"transaction": transaction()}, context()))
                self.assertEqual(response.status, status)
                self.assertNotIn("SECRET", json.dumps(response.to_dict()))
        for error, status in ((ModelTimeoutError(), "timeout"), (ModelSignatureError(), "error")):
            response = run(feature_rag(predictor=FixturePredictor(error=error)).call(
                "fraud_scoring", {"transaction": transaction()}, context()))
            self.assertEqual(response.status, status)

    def test_failure_isolation_between_calls(self):
        tools = feature_rag(predictor=FixturePredictor(error=ModelSignatureError()))
        self.assertEqual(run(tools.call("fraud_scoring", {"transaction": transaction()}, context())).status, "error")
        self.assertEqual(run(tools.call("feature_lookup", {"transaction": transaction()}, context())).status, "ok")
        self.assertEqual(run(tools.call("knowledge_search", {"query": "account takeover"}, context())).status, "ok")

    def test_read_only_tools_are_idempotent(self):
        tools = feature_rag()
        for name, arguments in (("feature_lookup", {"transaction": transaction()}),
                                ("fraud_scoring", {"transaction": transaction()}),
                                ("knowledge_search", {"query": "account takeover", "top_k": 2})):
            with self.subTest(name):
                first = run(tools.call(name, arguments, context())).to_dict()
                second = run(tools.call(name, arguments, context())).to_dict()
                self.assertEqual(first, second)

    def test_invalid_provenance_override_is_an_error(self):
        class BadProvenance(FeatureLookupTool):
            async def run(self, arguments, context):
                return ToolOutcome(result={}, version_overrides={"model_revision": "bad revision!"})

        response = run(executor(BadProvenance(FixtureFeatureProvider())).call(
            "feature_lookup", {"transaction": transaction()}, context()))
        self.assertEqual((response.status, response.result["error"]["code"]), ("error", "provenance_contract_error"))

    def test_logs_contain_metadata_but_not_arguments(self):
        stream = io.StringIO()
        logger = logging.getLogger("test.phase_d.tools")
        logger.addHandler(logging.StreamHandler(stream))
        logger.setLevel(logging.INFO)
        tools = feature_rag(logger=logger)
        run(tools.call("knowledge_search", {"query": "customer 0912345678 takeover"}, context()))
        run(tools.call("feature_lookup", {"transaction": transaction(user_id="user-SECRET")}, context()))
        text = stream.getvalue()
        self.assertIn('"tool_name": "knowledge_search"', text)
        self.assertNotIn("0912345678", text)
        self.assertNotIn("user-SECRET", text)


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the ewallet-ml interpreter")
class FeatureAndScoringToolTests(unittest.TestCase):
    def test_feature_lookup_returns_authorized_features_with_timestamps(self):
        response = run(feature_rag().call("feature_lookup", {"transaction": transaction()}, context()))
        self.assertEqual(response.status, "ok")
        features = response.result["historical_features"]
        self.assertEqual(list(features), list(HISTORICAL_FEATURE_COLUMNS))
        self.assertEqual(response.result["feature_contract"],
                         {"historical_returned": 31, "historical": 31, "request_time": 4, "derived_in_scoring_only": 6})
        self.assertFalse(response.result["materialization_verified"])
        self.assertTrue(all(item["event_timestamp"] for item in features.values()))

    def test_merchant_features_are_null_without_a_merchant(self):
        args = {"transaction": transaction(type="withdraw", merchant_id=None)}
        features = run(feature_rag().call("feature_lookup", args, context())).result["historical_features"]
        merchant = [name for name in features if name.startswith("merchant_behavior__")]
        self.assertTrue(merchant)
        self.assertTrue(all(features[name] == {"value": None, "event_timestamp": None} for name in merchant))

    def test_feature_name_partitions_and_boundaries(self):
        names = list(HISTORICAL_FEATURE_COLUMNS)
        tools = feature_rag()
        for valid in (names[:1], names):
            with self.subTest(count=len(valid)):
                response = run(tools.call("feature_lookup", {"transaction": transaction(), "feature_names": valid}, context()))
                self.assertEqual(list(response.result["historical_features"]), valid)
        for invalid in ([], names[:1] * 2, ["user_behavior__ssn"], names + ["amount"], "user_behavior__x"):
            with self.subTest(invalid=invalid):
                response = run(tools.call("feature_lookup", {"transaction": transaction(), "feature_names": invalid}, context()))
                self.assertEqual(response.status, "rejected")

    def test_transaction_schema_partitions(self):
        tools = feature_rag()
        for overrides in ({"amount": 0}, {"amount": float("nan")}, {"currency": "USD"},
                          {"merchant_id": None}, {"event_timestamp": "2026-10-30T12:00:00"},
                          {"type": "transfer", "counterparty_account_id": None}, {"unexpected": 1}):
            with self.subTest(overrides):
                response = run(tools.call("feature_lookup", {"transaction": transaction(**overrides)}, context()))
                self.assertEqual(response.status, "rejected")
        self.assertEqual(run(tools.call("feature_lookup", {"transaction": transaction(amount=0.01)}, context())).status, "ok")

    def test_non_numeric_backend_values_are_contract_errors(self):
        for bad in (True, "12", float("inf")):
            with self.subTest(bad=bad):
                provider = FixtureFeatureProvider(value_override={HISTORICAL_FEATURE_COLUMNS[0]: bad})
                response = run(feature_rag(provider=provider).call("feature_lookup", {"transaction": transaction()}, context()))
                self.assertEqual(response.status, "error")

    def test_fraud_scoring_propagates_model_provenance(self):
        response = run(feature_rag().call("fraud_scoring", {"transaction": transaction()}, context()))
        self.assertEqual(response.status, "ok")
        self.assertEqual(response.versions.model_revision, "fraud-xgb/7")
        self.assertEqual(response.result["fraud_probability"], 0.25)
        self.assertRegex(response.result["model_input_schema_sha256"], r"^[0-9a-f]{64}$")

    def test_fraud_scoring_rejects_other_models_and_transactions(self):
        for predictor, code in ((FixturePredictor(model=("fraud-xgb", "6")), "unexpected_model"),
                                (FixturePredictor(model=("other-model", "7")), "unexpected_model"),
                                (FixturePredictor(transaction_id="tx-other"), "prediction_identity_mismatch")):
            with self.subTest(code):
                response = run(feature_rag(predictor=predictor).call("fraud_scoring", {"transaction": transaction()}, context()))
                self.assertEqual((response.status, response.result["error"]["code"]), ("error", code))
        for name, version in (("", "7"), ("fraud-xgb", ""), ("fraud xgb", "7")):
            with self.subTest(name=name, version=version), self.assertRaises(ValueError):
                FraudScoringTool(FixturePredictor(), model_name=name, model_version=version)

    def test_readiness_reports_each_backend(self):
        tools = feature_rag(provider=FixtureFeatureProvider(ready=False), predictor=FixturePredictor(ready=False))
        report = run(tools.readiness())
        self.assertEqual(report["feature_lookup"]["code"], "feature_store_not_ready")
        self.assertEqual(report["fraud_scoring"]["code"], "model_service_not_ready")
        self.assertEqual(report["knowledge_search"], {"status": "ready"})
        failing = feature_rag(provider=FixtureFeatureProvider(ready=FeatureBackendError()))
        self.assertEqual(run(failing.readiness())["feature_lookup"]["code"], "feature_backend_error")


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the ewallet-ml interpreter")
class KnowledgeSearchToolTests(unittest.TestCase):
    def test_results_carry_manifest_bound_citations(self):
        response = run(feature_rag().call("knowledge_search", {"query": "account takeover", "top_k": 2}, context()))
        self.assertEqual(response.status, "ok")
        self.assertEqual(len(response.citations), len(response.result["results"]))
        self.assertTrue(all(c.source_checksum.startswith("sha256:") for c in response.citations))
        manifest = TrustedChunkIndex.from_manifest(MANIFEST).manifest
        self.assertEqual(response.versions.corpus_revision, manifest.corpus_revision)
        self.assertEqual(response.versions.feature_revision, manifest.feature_index_revision)
        self.assertNotIn("account takeover", json.dumps(response.to_dict()))
        self.assertEqual(response.result["trust_anchor"], "source_corpus")

    def test_input_partitions_and_boundaries(self):
        tools = feature_rag()
        valid = ({"query": "abc"}, {"query": "a" * 1000}, {"query": "abc", "top_k": 1},
                 {"query": "abc", "top_k": 5}, {"query": "abc", "min_score": 0.0}, {"query": "abc", "min_score": 1.0})
        invalid = ({"query": "ab"}, {"query": "a" * 1001}, {"query": "   "}, {"query": "abc\x00"},
                   {"query": "abc", "top_k": 0}, {"query": "abc", "top_k": 6}, {"query": "abc", "top_k": True},
                   {"query": "abc", "top_k": "3"}, {"query": "abc", "min_score": -0.01},
                   {"query": "abc", "min_score": 1.01}, {"query": "abc", "min_score": float("nan")},
                   {"query": "abc", "rerank": True})
        for arguments in valid:
            with self.subTest(valid=arguments):
                self.assertEqual(run(tools.call("knowledge_search", arguments, context())).status, "ok")
        for arguments in invalid:
            with self.subTest(invalid=arguments):
                self.assertEqual(run(tools.call("knowledge_search", arguments, context())).status, "rejected")

    def test_untrusted_retriever_output_is_rejected(self):
        def forge(hits):
            return (RagCitation(**{**hits[0].__dict__, "chunk_id": f"{hits[0].document_id}:9999"}),)

        cases = {
            "citation_integrity_error": forge,
            "retrieval_bound_exceeded": lambda hits: hits + hits[:1],
            "duplicate_citation": lambda hits: (hits[0], hits[0]),
            "retrieval_order_violation": lambda hits: tuple(reversed(hits)),
            "retrieval_threshold_violation": lambda hits: (RagCitation(**{**hits[0].__dict__, "score": 0.01}),),
        }
        for code, tamper in cases.items():
            with self.subTest(code):
                tools = feature_rag(retriever=FixtureRetriever(tamper=tamper))
                response = run(tools.call("knowledge_search", {"query": "takeover", "top_k": 3, "min_score": 0.5}, context()))
                self.assertEqual((response.status, response.result["error"]["code"]), ("error", code))
                self.assertEqual(response.citations, ())

    def test_production_retriever_is_explicitly_unavailable(self):
        retriever = UnavailableKnowledgeRetriever(TrustedChunkIndex.from_manifest(MANIFEST))
        tools = feature_rag(retriever=retriever)
        response = run(tools.call("knowledge_search", {"query": "takeover"}, context()))
        self.assertEqual((response.status, response.result["error"]["code"]), ("unavailable", "vector_search_unavailable"))
        self.assertIn("PUBLICATION_ONLY_RUNTIME_BLOCKED", response.result["error"]["message"])
        self.assertEqual(run(tools.readiness())["knowledge_search"]["status"], "not_ready")
        with self.assertRaises(ValueError):
            UnavailableKnowledgeRetriever(retriever.index, online_store="qdrant")


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the ewallet-ml interpreter")
class DriftToolTests(unittest.TestCase):
    def tool(self, base=None, values=None, sampler=None):
        return executor(DriftDetectionTool(FixtureBaselines(base or baseline()),
                                           sampler or FixtureSampler(values or [5, 15, 25, 35] * 5)))

    def test_matching_distribution_is_no_drift(self):
        response = run(self.tool().call("drift_detection", drift_args(), context()))
        self.assertEqual(response.status, "ok")
        self.assertEqual(response.result["drift_status"], "no_drift")
        self.assertAlmostEqual(response.result["psi"], 0.0, places=12)
        self.assertEqual(len(response.result["bins"]), 4)
        self.assertFalse(response.result["live_metrics_verified"])

    def test_psi_matches_hand_computation_and_classifies(self):
        values = [5] * 10 + [15] * 10
        expected = 2 * (0.5 - 0.25) * math.log(0.5 / 0.25) + 2 * (1e-4 - 0.25) * math.log(1e-4 / 0.25)
        psi, _ = population_stability_index(baseline(), tuple(values))
        self.assertAlmostEqual(psi, expected, places=12)
        self.assertEqual(run(self.tool(values=values).call("drift_detection", drift_args(), context())).result["drift_status"], "drift")
        warning = [5] * 7 + [15] * 5 + [25] * 4 + [35] * 4
        status = run(self.tool(values=warning, base=baseline(thresholds=(0.05, 2.0))).call(
            "drift_detection", drift_args(), context())).result["drift_status"]
        self.assertEqual(status, "warning")

    def test_threshold_boundaries_are_inclusive(self):
        values = [5] * 8 + [15] * 4 + [25] * 4 + [35] * 4
        psi, _ = population_stability_index(baseline(), tuple(values))
        for thresholds, status in (((psi / 2, psi), "drift"), ((psi / 2, math.nextafter(psi, math.inf)), "warning"),
                                   ((psi, psi * 2), "warning"), ((math.nextafter(psi, math.inf), psi * 2), "no_drift")):
            with self.subTest(status=status):
                result = run(self.tool(base=baseline(thresholds=thresholds), values=values).call(
                    "drift_detection", drift_args(), context())).result
                self.assertEqual(result["drift_status"], status)

    def test_without_approved_thresholds_result_is_unclassified(self):
        response = run(self.tool(base=baseline(thresholds=None), values=[5] * 20).call("drift_detection", drift_args(), context()))
        self.assertEqual(response.result["drift_status"], "unclassified")
        self.assertIsNone(response.result["thresholds"])
        self.assertGreater(response.result["psi"], 0)

    def test_minimum_sample_boundary(self):
        for count, status in ((9, "insufficient_data"), (10, "no_drift")):
            with self.subTest(count=count):
                values = ([5, 15, 25, 35] * 3)[:count] if count == 9 else [5, 15, 25, 35, 5, 15, 25, 35, 5, 15]
                result = run(self.tool(values=values, base=baseline(thresholds=(1.0, 2.0))).call(
                    "drift_detection", drift_args(), context())).result
                self.assertEqual(result["drift_status"], status)

    def test_bin_edges_are_right_closed(self):
        _, bins = population_stability_index(baseline(), (10.0, 10.000001, 30.0, 30.5))
        self.assertEqual([b["current_proportion"] for b in bins], [0.25, 0.25, 0.25, 0.25])

    def test_input_partitions(self):
        start = EVENT_TIME - timedelta(days=7)
        invalid = (
            drift_args(feature_name="ssn"), drift_args(baseline_id="latest"), drift_args(baseline_id="bad id"),
            drift_args(current_window={"start": EVENT_TIME.isoformat(), "end": start.isoformat()}),
            drift_args(current_window={"start": EVENT_TIME.isoformat(), "end": EVENT_TIME.isoformat()}),
            drift_args(current_window={"start": (EVENT_TIME - timedelta(days=31, seconds=1)).isoformat(), "end": EVENT_TIME.isoformat()}),
            drift_args(current_window={"start": "2026-10-23T12:00:00", "end": "2026-10-30T12:00:00"}),
            drift_args(extra=True),
        )
        for arguments in invalid:
            with self.subTest(arguments):
                self.assertEqual(run(self.tool().call("drift_detection", arguments, context())).status, "rejected")
        # Exactly 31 days, starting where the reference window ends (adjacent, not overlapping).
        exactly_31_days = drift_args(current_window={"start": (EVENT_TIME - timedelta(days=31)).isoformat(), "end": EVENT_TIME.isoformat()})
        adjacent = baseline(reference_end=EVENT_TIME - timedelta(days=31))
        self.assertEqual(run(self.tool(base=adjacent).call("drift_detection", exactly_31_days, context())).status, "ok")

    def test_baseline_and_window_identity(self):
        response = run(self.tool().call("drift_detection", drift_args(baseline_id="baseline-other-v1"), context()))
        self.assertEqual(response.result["error"]["code"], "unknown_baseline")
        overlap = drift_args(current_window={"start": (EVENT_TIME - timedelta(days=40)).isoformat(),
                                             "end": (EVENT_TIME - timedelta(days=35)).isoformat()})
        self.assertEqual(run(self.tool().call("drift_detection", overlap, context())).result["error"]["code"],
                         "window_overlaps_reference")
        wrong = FixtureSampler([5] * 20, identity=("user_behavior__user_tx_count_5m", EVENT_TIME - timedelta(days=7), EVENT_TIME))
        self.assertEqual(run(self.tool(sampler=wrong).call("drift_detection", drift_args(), context())).result["error"]["code"],
                         "window_identity_mismatch")
        for values in ([5, float("nan")] * 10, [5, True] * 10):
            with self.subTest(values=values[:2]):
                response = run(self.tool(values=values).call("drift_detection", drift_args(), context()))
                self.assertEqual(response.result["error"]["code"], "non_finite_feature_value")
        self.assertEqual(run(self.tool(sampler=FixtureSampler([5] * 20, revision="latest window")).call(
            "drift_detection", drift_args(), context())).result["error"]["code"], "window_identity_mismatch")

    def test_baseline_validation(self):
        invalid = (
            {"cut_points": (20.0, 10.0, 30.0)}, {"cut_points": ()}, {"reference_proportions": (0.5, 0.5, 0.5, 0.5)},
            {"reference_proportions": (0.5, 0.5)}, {"reference_proportions": (1.5, -0.5, 0.0, 0.0)},
            {"smoothing": 0.0}, {"smoothing": 0.02}, {"min_current_count": 0}, {"thresholds": (0.25, 0.1)},
            {"thresholds": (0.0, 0.1)}, {"method": "ks"}, {"feature_name": "ssn"}, {"dataset_revision": ""},
            {"reference_end": EVENT_TIME - timedelta(days=61)}, {"reference_start": datetime(2026, 8, 1)},
            {"cut_points": (10.0, float("inf"), 30.0)},
        )
        for overrides in invalid:
            with self.subTest(overrides), self.assertRaises(ValueError):
                baseline(**overrides)

    def test_psi_properties_on_seeded_random_inputs(self):
        # Seeded property checks (Hypothesis is not installed in this environment).
        generator = random.Random(20261031)
        base = baseline()
        for _ in range(200):
            values = tuple(generator.uniform(-10, 50) for _ in range(generator.randint(1, 60)))
            psi, bins = population_stability_index(base, values)
            shuffled = list(values)
            generator.shuffle(shuffled)
            self.assertGreaterEqual(psi, 0.0)
            self.assertAlmostEqual(sum(b["current_proportion"] for b in bins), 1.0, places=12)
            self.assertAlmostEqual(psi, population_stability_index(base, tuple(shuffled))[0], places=12)
            self.assertAlmostEqual(psi, sum(b["contribution"] for b in bins), places=12)

    def test_production_drift_backends_are_unavailable(self):
        tools = executor(DriftDetectionTool(UnavailableBaselineStore(), UnavailableWindowSampler()))
        response = run(tools.call("drift_detection", drift_args(), context()))
        self.assertEqual((response.status, response.result["error"]["code"]), ("unavailable", "drift_baseline_unavailable"))
        self.assertEqual(run(tools.readiness())["drift_detection"]["status"], "not_ready")


async def asgi(app, method, path, body=None, headers=None, raw=None):
    sent = []
    encoded = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    messages = [{"type": "http.request", "body": encoded, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    all_headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", **(headers or {})}
    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in all_headers.items()],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80), "root_path": "",
    }, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    payload = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], json.loads(payload) if payload else None


class FixtureAuthenticator:
    """TEST FIXTURE: a static token table standing in for the undecided mechanism."""

    tokens = {"fixture-token-specialist": SUBJECT, "fixture-token-other": "spiffe-test/drift-specialist"}

    async def authenticate(self, headers):
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not token:
            raise AuthenticationError("authentication_required")
        if token not in self.tokens:
            raise AuthenticationError("authentication_failed")
        return Principal(subject=self.tokens[token], mechanism="test-fixture")


def tool_app(executor, grants=None, **kwargs):
    grants = grants or ToolGrants({SUBJECT: frozenset({"feature_lookup", "fraud_scoring", "knowledge_search"})})
    return create_tool_app(service_name="feature-rag-mcp", executor=executor,
                           authenticator=FixtureAuthenticator(), grants=grants, **kwargs)


def rpc(method, params=None, message_id=1):
    message = {"jsonrpc": "2.0", "method": method, "id": message_id}
    if params is not None:
        message["params"] = params
    return message


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the ewallet-ml interpreter")
class ServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = tool_app(feature_rag())

    def call(self, *args, **kwargs):
        return run(asgi(self.app, *args, **kwargs))

    def test_versions_come_from_deployment_identity(self):
        self.assertEqual(versions_from_env(ENV).git_sha, "a" * 40)
        for missing in ("EWALLET_GIT_SHA", "EWALLET_IMAGE_DIGEST"):
            with self.subTest(missing), self.assertRaises(ConfigurationError):
                versions_from_env({k: v for k, v in ENV.items() if k != missing})
        with self.assertRaises(ConfigurationError):
            versions_from_env({**ENV, "EWALLET_IMAGE_DIGEST": "latest"})

    def test_health_probes(self):
        self.assertEqual(self.call("GET", "/health/live"), (200, {"status": "ok", "service": "feature-rag-mcp"}))
        status, body = self.call("GET", "/health/ready")
        self.assertEqual((status, body["status"]), (200, "ready"))

    def test_production_drift_transport_reports_missing_authentication(self):
        # Readiness is transport-only (2026-10-31): a missing drift baseline no longer
        # gates it, but production has no approved authenticator, which is reported.
        status, body = run(asgi(create_drift_app(ENV), "GET", "/health/ready"))
        self.assertEqual((status, body), (503, {"status": "not_ready", "service": "drift-mcp", "scope": "transport",
                                                "reason": "authentication_not_configured"}))

    def test_production_wiring_fails_closed_and_logs_rejections(self):
        # Found on the kind run: without a handler the INFO events were dropped.
        # Production has no approved authenticator yet, so every tool call is denied.
        app = create_drift_app(ENV)
        handlers = [h for h in logging.getLogger("ewallet.llm_agent.tools").handlers
                    if getattr(h, "_ewallet_audit", False)]
        self.assertEqual(len(handlers), 1)
        create_drift_app(ENV)
        self.assertEqual(len([h for h in logging.getLogger("ewallet.llm_agent.tools").handlers
                              if getattr(h, "_ewallet_audit", False)]), 1)
        stream = io.StringIO()
        previous = handlers[0].setStream(stream)
        try:
            status, body = run(asgi(app, "POST", "/v1/tools/drift_detection", {
                "feature_name": "amount", "baseline_id": "psi-amount-v1",
                "current_window": {"start": "2026-10-20T00:00:00Z", "end": "2026-10-27T00:00:00Z"},
            }, headers={"x-ewallet-actor": "agent:test", "authorization": "Bearer forged-secret-token",
                        "x-request-id": "req-audit"}))
        finally:
            handlers[0].setStream(previous)
        self.assertEqual((status, body["code"]), (401, "authentication_not_configured"))
        event = json.loads(stream.getvalue().strip())
        self.assertEqual((event["event"], event["status"], event["code"]),
                         ("request_rejected", 401, "authentication_not_configured"))
        for leaked in ("amount", "forged-secret-token", "agent:test"):
            self.assertNotIn(leaked, stream.getvalue() + json.dumps(body))

    def test_typed_rest_route(self):
        status, body = self.call("POST", "/v1/tools/fraud_scoring", {"transaction": transaction()},
                                 headers={**ACTOR, "x-request-id": "rest-1"})
        self.assertEqual((status, body["status"], body["request_id"]), (200, "ok", "rest-1"))
        self.assertEqual(body["versions"]["image_digest"], ENV["EWALLET_IMAGE_DIGEST"])

    def test_rest_boundary_failures(self):
        status, body = self.call("POST", "/v1/tools/feature_lookup", {"transaction": transaction()})
        self.assertEqual((status, body["code"]), (401, "authentication_required"))
        status, body = self.call("POST", "/v1/tools/feature_lookup", {"transaction": transaction(user_id="SECRET-1", amount=-1)}, headers=ACTOR)
        self.assertEqual((status, body["code"]), (422, "invalid_arguments"))
        self.assertNotIn("SECRET-1", json.dumps(body))
        status, body = self.call("POST", "/v1/tools/feature_lookup", {"transaction": transaction()},
                                 headers={**ACTOR, "x-ewallet-timeout-ms": "99"})
        self.assertEqual((status, body["code"]), (400, "invalid_request_context"))
        self.assertEqual(self.call("POST", "/v1/tools/drift_detection", {}, headers=ACTOR)[0], 404)
        status, body = self.call("POST", "/v1/tools/feature_lookup", {"transaction": transaction()},
                                 headers={"x-request-id": "r" * 129})
        self.assertEqual((status, body["code"], body["request_id"]), (401, "authentication_required", "unknown"))

    def test_rest_status_codes_follow_tool_status(self):
        app = tool_app(feature_rag(provider=FixtureFeatureProvider(error=FeatureBackendError())))
        status, body = run(asgi(app, "POST", "/v1/tools/feature_lookup", {"transaction": transaction()}, headers=ACTOR))
        self.assertEqual((status, body["status"]), (503, "unavailable"))

    def test_mcp_lifecycle_and_tools(self):
        status, body = self.call("POST", "/mcp", rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                                    "clientInfo": {"name": "t", "version": "1"}}),
                                 headers=ACTOR)
        self.assertEqual((status, body["result"]["protocolVersion"]), (200, "2025-03-26"))
        self.assertEqual(body["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(self.call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                   headers=ACTOR)[0], 202)
        status, body = self.call("POST", "/mcp", rpc("tools/list"), headers=ACTOR)
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        self.assertEqual(set(tools), {"feature_lookup", "fraud_scoring", "knowledge_search"})
        self.assertEqual(tools["knowledge_search"]["inputSchema"]["properties"]["top_k"]["maximum"], 5)
        status, body = self.call("POST", "/mcp", rpc("tools/call", {"name": "knowledge_search", "arguments": {"query": "takeover"}}),
                                 headers=ACTOR)
        result = body["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertTrue(result["structuredContent"]["citations"])

    def test_mcp_errors(self):
        cases = (
            (rpc("tools/call", {"name": "shell", "arguments": {}}), 200, -32602),
            (rpc("resources/list"), 200, -32601),
            (rpc("tools/call", ["not", "an", "object"]), 200, -32602),
            ([rpc("ping")], 400, -32600),
            ({"jsonrpc": "1.0", "method": "ping", "id": 1}, 400, -32600),
        )
        for message, status, code in cases:
            with self.subTest(message):
                actual_status, body = self.call("POST", "/mcp", message, headers=ACTOR)
                self.assertEqual((actual_status, body["error"]["code"]), (status, code))
        status, body = self.call("POST", "/mcp", raw=b"{not json", headers=ACTOR)
        self.assertEqual((status, body["error"]["code"]), (400, -32700))
        status, _ = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "mcp-protocol-version": "2024-01-01"})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers=ACTOR)[1]["result"], {})
        for message in (rpc("ping"), rpc("tools/list"),
                        rpc("tools/call", {"name": "knowledge_search", "arguments": {"query": "takeover"}})):
            status, body = self.call("POST", "/mcp", message)
            self.assertEqual((status, body["code"]), (401, "authentication_required"))

    def test_mcp_tool_failure_is_reported_in_band(self):
        app = tool_app(feature_rag(retriever=UnavailableKnowledgeRetriever(TrustedChunkIndex.from_manifest(MANIFEST))))
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/call", {"name": "knowledge_search",
                                                                       "arguments": {"query": "takeover"}}), headers=ACTOR))
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertEqual(body["result"]["structuredContent"]["status"], "unavailable")

    def test_concurrent_requests_through_the_app(self):
        async def burst():
            return await asyncio.gather(*(
                asgi(self.app, "POST", "/v1/tools/feature_lookup", {"transaction": transaction(transaction_id=f"tx-{i}")},
                     headers={**ACTOR, "x-request-id": f"c-{i}"}) for i in range(8)))

        results = run(burst())
        self.assertEqual({status for status, _ in results}, {200})
        self.assertEqual({body["result"]["transaction_id"] for _, body in results}, {f"tx-{i}" for i in range(8)})


if __name__ == "__main__":
    unittest.main()
