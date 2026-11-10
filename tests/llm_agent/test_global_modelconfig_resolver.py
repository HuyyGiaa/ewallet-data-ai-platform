"""BYO agents resolve the global kagent ModelConfig (edai-2 global ModelConfig row)."""

from __future__ import annotations

import copy
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from ai_platform.llm_agent import model_config, nl_intake
from ai_platform.llm_agent.model_config import (
    ModelConfigError,
    fetch_model_config,
    parse_model_config,
    resolve_from_env,
)
from ai_platform.llm_agent.nl_intake import OpenAIToolPlanner, TOOL_NAME, plan, planner_from_env
from tests.llm_agent.test_n11_nl_intake import PROMPT, RecordingLLM, run


NAME, NAMESPACE = "ewallet-global-llm", "ewallet-agents"
SECRET_REF = ("agentgateway-client-key", "api-key")
KUBE_TOKEN = "kube-token-under-test"
LLM_KEY = "gateway-client-key-under-test"
# Shape of the live object read back on GKE (kubectl get modelconfig ewallet-global-llm -o json).
LIVE = {
    "apiVersion": "kagent.dev/v1alpha2",
    "kind": "ModelConfig",
    "metadata": {"name": NAME, "namespace": NAMESPACE, "uid": "663305be-f4ce-49f2-ac7e-af91c22bd162",
                 "generation": 1, "resourceVersion": "1793554505356463008"},
    "spec": {"apiKeySecret": "agentgateway-client-key", "apiKeySecretKey": "api-key",
             "model": "ewallet-fraud-llm", "provider": "OpenAI",
             "openAI": {"apiFormat": "chatCompletions",
                        "baseUrl": "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1", "timeout": 200}},
    "status": {"conditions": [{"type": "Accepted", "status": "True", "reason": "ModelConfigReconciled"}],
               "observedGeneration": 1},
}


def variant(**changes):
    doc = copy.deepcopy(LIVE)
    for path, value in changes.items():
        node = doc
        *parents, leaf = path.split("__")
        for part in parents:
            node = node[part]
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return doc


class KubeAPI:
    """Records requests; answers with a fixed status and body."""

    def __init__(self, body=LIVE, status=200, statuses=None):
        self.body, self.statuses = body, list(statuses or [status])
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(status, json=self.body if status == 200 else {"kind": "Status", "code": status})
        return httpx.MockTransport(handler)


class TokenDir:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name)
        (path / "token").write_text(KUBE_TOKEN + "\n")
        (path / "ca.crt").write_text("unused with a mock transport\n")
        (path / "namespace").write_text(NAMESPACE)
        return path

    def __exit__(self, *exc):
        self._tmp.cleanup()


def env(token_dir, **extra):
    base = {"EWALLET_LLM_MODELCONFIG_NAME": NAME, "EWALLET_KUBE_API_TOKEN_DIR": str(token_dir),
            "EWALLET_LLM_API_KEY_SECRET_REF": "agentgateway-client-key/api-key",
            "KUBERNETES_SERVICE_HOST": "34.118.224.1", "KUBERNETES_SERVICE_PORT": "443"}
    base.update(extra)
    return base


class ParseTests(unittest.TestCase):
    def parse(self, doc):
        return parse_model_config(doc, name=NAME, namespace=NAMESPACE, secret_ref=SECRET_REF)

    def test_live_object_resolves_to_the_gateway_route(self):
        resolved = self.parse(LIVE)
        self.assertEqual(resolved.model, "ewallet-fraud-llm")
        self.assertEqual(resolved.base_url, "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1")
        self.assertEqual((resolved.provider, resolved.timeout_seconds, resolved.generation), ("OpenAI", 200.0, 1))
        self.assertEqual((resolved.api_key_secret, resolved.api_key_secret_key), SECRET_REF)

    def test_invalid_provider_is_rejected(self):
        for provider in ("Anthropic", "Ollama", "openai", None):
            with self.subTest(provider), self.assertRaises(ModelConfigError) as caught:
                self.parse(variant(spec__provider=provider))
            self.assertEqual(caught.exception.code, "modelconfig_provider_unsupported")

    def test_unauthorized_endpoints_are_rejected(self):
        for url in ("http://vllm-primary.ewallet-agents.svc.cluster.local:8000/v1",
                    "https://agentgateway.ewallet-agents.svc.cluster.local:3000/v1",
                    "http://agentgateway.ewallet-agents.svc.cluster.local:3001/v1",
                    "http://agentgateway.ewallet-llm-gpu.svc.cluster.local:3000/v1",
                    "http://llmd-envoy-o2.ewallet-llm-gpu.svc.cluster.local:8080/v1",
                    "http://user:pw@agentgateway.ewallet-agents.svc.cluster.local:3000/v1",
                    "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v2",
                    "http://api.openai.com/v1"):
            with self.subTest(url), self.assertRaises(ModelConfigError) as caught:
                self.parse(variant(spec__openAI__baseUrl=url))
            self.assertEqual(caught.exception.code, "modelconfig_endpoint_unauthorized")

    def test_secret_binding_must_match_the_mounted_secret(self):
        for change in ({"spec__apiKeySecret": "vllm-api-key"}, {"spec__apiKeySecretKey": "token"},
                       {"spec__apiKeySecret": None}):
            with self.subTest(change), self.assertRaises(ModelConfigError) as caught:
                self.parse(variant(**change))
            self.assertEqual(caught.exception.code, "modelconfig_secret_mismatch")

    def test_unaccepted_or_unreconciled_config_is_rejected(self):
        rejected = variant(status__conditions=[{"type": "Accepted", "status": "False"}])
        stale = variant(metadata__generation=2)
        for doc in (rejected, stale, variant(status=None)):
            with self.assertRaises(ModelConfigError) as caught:
                self.parse(doc)
            self.assertEqual(caught.exception.code, "modelconfig_not_accepted")

    def test_malformed_fields_are_rejected(self):
        cases = [variant(kind="Agent"), variant(metadata__name="other"), variant(spec__model="bad model"),
                 variant(spec__model=None), variant(spec__openAI__timeout=0), variant(spec__openAI__timeout=900),
                 variant(spec__openAI__timeout=True), variant(spec__openAI__apiFormat="responses"),
                 variant(metadata__deletionTimestamp="2026-11-09T00:00:00Z")]
        for doc in cases:
            with self.assertRaises(ModelConfigError) as caught:
                self.parse(doc)
            self.assertEqual(caught.exception.code, "modelconfig_invalid")


class FetchTests(unittest.TestCase):
    def test_get_uses_the_named_resource_and_the_projected_token(self):
        api = KubeAPI()
        with TokenDir() as token_dir:
            resolved = resolve_from_env(env(token_dir), transport=api.transport())
        self.assertEqual(resolved.model, "ewallet-fraud-llm")
        (request,) = api.requests
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url),
                         "https://34.118.224.1/apis/kagent.dev/v1alpha2/namespaces/ewallet-agents/modelconfigs/ewallet-global-llm")
        self.assertEqual(request.headers["authorization"], f"Bearer {KUBE_TOKEN}")

    def test_missing_modelconfig_fails_closed(self):
        with TokenDir() as token_dir, self.assertRaises(ModelConfigError) as caught:
            resolve_from_env(env(token_dir), transport=KubeAPI(status=404).transport())
        self.assertEqual(caught.exception.code, "modelconfig_not_found")

    def test_insufficient_rbac_fails_closed_without_retry(self):
        for status in (401, 403):
            api = KubeAPI(status=status)
            with TokenDir() as token_dir, self.assertRaises(ModelConfigError) as caught:
                resolve_from_env(env(token_dir), transport=api.transport())
            self.assertEqual(caught.exception.code, "modelconfig_forbidden")
            self.assertEqual(len(api.requests), 1)

    def test_api_server_errors_are_retried_then_fail_closed(self):
        api = KubeAPI(statuses=[503, 200])
        with TokenDir() as token_dir:
            fetch_model_config(NAME, NAMESPACE, token_dir=token_dir, api_host="h", api_port=443,
                               transport=api.transport(), backoff_seconds=0)
        self.assertEqual(len(api.requests), 2)
        api = KubeAPI(status=500)
        with TokenDir() as token_dir, self.assertRaises(ModelConfigError) as caught:
            fetch_model_config(NAME, NAMESPACE, token_dir=token_dir, api_host="h", api_port=443,
                               transport=api.transport(), backoff_seconds=0)
        self.assertEqual((caught.exception.code, len(api.requests)), ("modelconfig_unavailable", 3))

    def test_missing_kubernetes_credentials_fail_before_any_request(self):
        api = KubeAPI()
        with TokenDir() as token_dir:
            (token_dir / "token").unlink()
            with self.assertRaises(ModelConfigError) as caught:
                resolve_from_env(env(token_dir), transport=api.transport())
        self.assertEqual((caught.exception.code, api.requests), ("modelconfig_credentials_unavailable", []))
        with TokenDir() as token_dir:
            (token_dir / "ca.crt").unlink()
            with self.assertRaises(ModelConfigError) as caught:
                resolve_from_env(env(token_dir))       # real transport: the CA is mandatory
        self.assertEqual(caught.exception.code, "modelconfig_credentials_unavailable")

    def test_misconfigured_pod_env_fails_closed(self):
        with TokenDir() as token_dir:
            for change in ({"EWALLET_LLM_MODELCONFIG_NAME": ""}, {"EWALLET_LLM_API_KEY_SECRET_REF": "no-key"},
                           {"KUBERNETES_SERVICE_HOST": ""}):
                with self.subTest(change), self.assertRaises(ModelConfigError) as caught:
                    resolve_from_env(env(token_dir, **change), transport=KubeAPI().transport())
                self.assertEqual(caught.exception.code, "modelconfig_misconfigured")


class PlannerFromModelConfigTests(unittest.TestCase):
    """planner_from_env with EWALLET_LLM_CONFIG_SOURCE=modelconfig, as rendered by the chart."""

    def environ(self, token_dir, **extra):
        return {**env(token_dir), "EWALLET_NL_INTAKE_ENABLED": "true", "EWALLET_LLM_CONFIG_SOURCE": "modelconfig",
                "EWALLET_LLM_TIMEOUT_SECONDS": "190", "EWALLET_LLM_API_KEY": LLM_KEY, **extra}

    def build(self, token_dir, api=None, **extra):
        api = api or KubeAPI()
        real = model_config.resolve_from_env
        with mock.patch.dict(os.environ, self.environ(token_dir, **extra), clear=True), \
                mock.patch.object(nl_intake, "resolve_from_env", lambda: real(os.environ, transport=api.transport())):
            return planner_from_env()

    def test_planner_takes_model_url_and_timeout_ceiling_from_the_cr(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger("ewallet.modelconfig").addHandler(handler)
        try:
            with TokenDir() as token_dir:
                planner = self.build(token_dir, KubeAPI(variant(spec__model="ewallet-fraud-llm-v2",
                                                                spec__openAI__timeout=60)))
        finally:
            logging.getLogger("ewallet.modelconfig").removeHandler(handler)
        self.assertIsInstance(planner, OpenAIToolPlanner)
        self.assertEqual(planner._model, "ewallet-fraud-llm-v2")
        self.assertEqual(planner._url, "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1/chat/completions")
        self.assertEqual(planner._timeout, 60.0)
        record = json.loads(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual((record["event"], record["model"], record["uid"]),
                         ("modelconfig.resolved", "ewallet-fraud-llm-v2", LIVE["metadata"]["uid"]))
        self.assertNotIn(LLM_KEY, stream.getvalue())
        self.assertNotIn(KUBE_TOKEN, stream.getvalue())

    def test_agent_deadline_below_the_cr_timeout_is_kept(self):
        with TokenDir() as token_dir:
            self.assertEqual(self.build(token_dir)._timeout, 190.0)

    def test_second_authority_in_env_stops_the_agent(self):
        for name in ("EWALLET_LLM_BASE_URL", "EWALLET_LLM_MODEL"):
            with TokenDir() as token_dir, self.subTest(name), self.assertRaises(ModelConfigError) as caught:
                self.build(token_dir, **{name: "x"})
            self.assertEqual(caught.exception.code, "modelconfig_misconfigured")

    def test_missing_gateway_key_stops_the_agent_before_the_api_call(self):
        api = KubeAPI()
        with TokenDir() as token_dir, self.assertRaises(ModelConfigError) as caught:
            self.build(token_dir, api, EWALLET_LLM_API_KEY="")
        self.assertEqual((caught.exception.code, api.requests), ("llm_credential_unavailable", []))

    def test_cr_failures_propagate_to_the_agent_constructor(self):
        for api, code in ((KubeAPI(status=403), "modelconfig_forbidden"), (KubeAPI(status=404), "modelconfig_not_found"),
                          (KubeAPI(variant(spec__provider="Anthropic")), "modelconfig_provider_unsupported")):
            with TokenDir() as token_dir, self.subTest(code), self.assertRaises(ModelConfigError) as caught:
                self.build(token_dir, api)
            self.assertEqual(caught.exception.code, code)

    def test_unknown_source_is_rejected_and_env_source_is_unchanged(self):
        with TokenDir() as token_dir, self.assertRaises(ModelConfigError):
            self.build(token_dir, EWALLET_LLM_CONFIG_SOURCE="crd")
        legacy = {"EWALLET_NL_INTAKE_ENABLED": "true", "EWALLET_LLM_MODEL": "ewallet-fraud-llm"}
        with mock.patch.dict(os.environ, legacy, clear=True), \
                mock.patch.object(nl_intake, "resolve_from_env", side_effect=AssertionError("no API call")):
            planner = planner_from_env()
        self.assertEqual((planner._model, planner._url),
                         ("ewallet-fraud-llm", "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1/chat/completions"))

    def test_intake_disabled_never_reads_the_cr(self):
        with TokenDir() as token_dir:
            api = KubeAPI()
            self.assertIsNone(self.build(token_dir, api, EWALLET_NL_INTAKE_ENABLED="false"))
        self.assertEqual(api.requests, [])

    def test_tool_selection_is_unchanged_with_the_resolved_planner(self):
        with TokenDir() as token_dir:
            planner = self.build(token_dir)
        llm = RecordingLLM()
        planner._transport = llm.transport()
        with mock.patch.dict(os.environ, {"EWALLET_LLM_API_KEY": LLM_KEY}):
            result = run(plan(PROMPT, planner=planner, context_id="ctx-1", message_id="msg-1"))
        (sent,) = llm.requests
        self.assertEqual(str(sent.url), "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1/chat/completions")
        self.assertEqual(sent.headers["authorization"], f"Bearer {LLM_KEY}")
        body = json.loads(sent.content)
        self.assertEqual((body["model"], [t["function"]["name"] for t in body["tools"]]), ("ewallet-fraud-llm", [TOOL_NAME]))
        self.assertEqual(result.selection.name, TOOL_NAME)
        self.assertEqual(result.request.transaction.transaction_id, "dc58ff4e-b5f7-4486-b1f8-4c31383741a9")

    def test_ab_experiment_keeps_variant_models_on_the_resolved_route(self):
        ab = json.dumps({"experiment_id": "exp-1", "salt": "s", "variants": [
            {"name": "baseline", "weight": 50, "model": "ewallet-fraud-llm", "temperature": 0, "max_tokens": 384},
            {"name": "candidate", "weight": 50, "model": "ewallet-fraud-llm-candidate", "temperature": 0,
             "max_tokens": 256}]})
        with TokenDir() as token_dir:
            planner = self.build(token_dir, EWALLET_AGENT_AB_CONFIG=ab)
        for name in ("baseline", "candidate"):
            self.assertEqual(planner._planners[name]._url,
                             "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
