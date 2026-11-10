"""Startup-time resolver: BYO agents read the global kagent ModelConfig.

kagent 0.10.3 Declarative Agents reference a ModelConfig through
``spec.declarative.modelConfig``; BYO Agents have no such field (``spec.byo`` only carries
``deployment``). This module closes that gap without a controller: when the agent starts,
it reads the ``ModelConfig`` named in ``EWALLET_LLM_MODELCONFIG_NAME`` from its own
namespace through the Kubernetes API (``get`` on that one resource name, nothing else) and
takes the provider, model, base URL and timeout from it.

Fail closed: a missing, unreadable, rejected or inconsistent ModelConfig stops the agent
instead of falling back to defaults. The credential stays a Secret reference resolved by
the kubelet (``EWALLET_LLM_API_KEY``); this module never reads a Secret and never logs a
credential. It checks that the ModelConfig still names the Secret the pod was started
with, so a ModelConfig that moved to another Secret cannot be served with a stale key.

A ModelConfig change reaches BYO agents on their next start (``kubectl rollout restart``);
there is no watch.

Only the standard library and ``httpx`` are imported, so the module can also be loaded on
its own (the isolated canary in ``infra/k8s/modelconfig-canary/`` does that).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import quote, urlparse

import httpx


APPROVED_LLM_HOST = "agentgateway.ewallet-agents.svc.cluster.local"
APPROVED_LLM_PORT = 3000
APPROVED_LLM_BASE_URL = f"http://{APPROVED_LLM_HOST}:{APPROVED_LLM_PORT}/v1"
MAX_LLM_TIMEOUT_SECONDS = 200.0

API_GROUP_VERSION = "kagent.dev/v1alpha2"
SUPPORTED_PROVIDER = "OpenAI"
SUPPORTED_API_FORMATS = ("", "chatCompletions")
DEFAULT_TOKEN_DIR = "/var/run/secrets/ewallet/kube-api"
MAX_MODELCONFIG_BYTES = 65_536
KUBE_API_TIMEOUT_SECONDS = 5.0
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,64}")

_LOG = logging.getLogger("ewallet.modelconfig")


class ModelConfigError(RuntimeError):
    """The global ModelConfig cannot be used; ``code`` is a stable, credential-free reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ResolvedModelConfig:
    name: str
    namespace: str
    uid: str
    generation: int
    resource_version: str
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    api_key_secret: str
    api_key_secret_key: str

    def audit(self) -> dict[str, object]:
        """Everything here is non-secret: names, endpoint, model and object identity."""

        return {"event": "modelconfig.resolved", **asdict(self)}


def validate_base_url(value: str) -> None:
    """Only the in-cluster agentgateway route; never a model server or an external host."""

    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != APPROVED_LLM_HOST
        or parsed.port != APPROVED_LLM_PORT
        or parsed.path != "/v1"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("LLM base URL must be the approved in-cluster agentgateway route")


def _secret_ref(value: str) -> tuple[str, str]:
    name, sep, key = value.partition("/")
    if not sep or not _NAME.fullmatch(name) or not re.fullmatch(r"[-._A-Za-z0-9]{1,253}", key):
        raise ModelConfigError("modelconfig_misconfigured", "EWALLET_LLM_API_KEY_SECRET_REF must be <secret>/<key>")
    return name, key


def parse_model_config(
    document: Mapping[str, object], *, name: str, namespace: str, secret_ref: tuple[str, str],
) -> ResolvedModelConfig:
    """Validate one ModelConfig object as returned by the API server."""

    def bad(code: str, message: str) -> ModelConfigError:
        return ModelConfigError(code, message)

    if document.get("apiVersion") != API_GROUP_VERSION or document.get("kind") != "ModelConfig":
        raise bad("modelconfig_invalid", f"object is not a {API_GROUP_VERSION} ModelConfig")
    meta = document.get("metadata") or {}
    spec = document.get("spec") or {}
    status = document.get("status") or {}
    if not isinstance(meta, Mapping) or not isinstance(spec, Mapping) or not isinstance(status, Mapping):
        raise bad("modelconfig_invalid", "metadata, spec and status must be objects")
    if meta.get("name") != name or meta.get("namespace") != namespace:
        raise bad("modelconfig_invalid", "object identity does not match the requested ModelConfig")
    if meta.get("deletionTimestamp"):
        raise bad("modelconfig_invalid", "ModelConfig is being deleted")

    provider = spec.get("provider")
    if provider != SUPPORTED_PROVIDER:
        raise bad("modelconfig_provider_unsupported", f"provider must be {SUPPORTED_PROVIDER}")
    model = spec.get("model")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise bad("modelconfig_invalid", "spec.model is missing or not a logical model name")
    openai = spec.get("openAI") or {}
    if not isinstance(openai, Mapping):
        raise bad("modelconfig_invalid", "spec.openAI must be an object")
    if openai.get("apiFormat", "") not in SUPPORTED_API_FORMATS:
        raise bad("modelconfig_invalid", "spec.openAI.apiFormat must be chatCompletions")
    base_url = openai.get("baseUrl")
    if not isinstance(base_url, str):
        raise bad("modelconfig_invalid", "spec.openAI.baseUrl is missing")
    try:
        validate_base_url(base_url)
    except ValueError as exc:
        raise bad("modelconfig_endpoint_unauthorized", str(exc)) from exc
    timeout = openai.get("timeout")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_LLM_TIMEOUT_SECONDS:
        raise bad("modelconfig_invalid", "spec.openAI.timeout must be between 1 and 200 seconds")

    if (spec.get("apiKeySecret"), spec.get("apiKeySecretKey")) != secret_ref:
        raise bad("modelconfig_secret_mismatch",
                  "ModelConfig names a different API key Secret than the one mounted in this pod")

    accepted = [c for c in status.get("conditions") or [] if isinstance(c, Mapping) and c.get("type") == "Accepted"]
    if not accepted or accepted[0].get("status") != "True":
        raise bad("modelconfig_not_accepted", "kagent has not accepted this ModelConfig")
    generation = meta.get("generation")
    if not isinstance(generation, int) or status.get("observedGeneration") != generation:
        raise bad("modelconfig_not_accepted", "kagent has not reconciled the current ModelConfig generation")

    return ResolvedModelConfig(
        name=name, namespace=namespace, uid=str(meta.get("uid", "")), generation=generation,
        resource_version=str(meta.get("resourceVersion", "")), provider=provider, model=model,
        base_url=base_url, timeout_seconds=float(timeout),
        api_key_secret=secret_ref[0], api_key_secret_key=secret_ref[1],
    )


def fetch_model_config(
    name: str,
    namespace: str,
    *,
    token_dir: Path,
    api_host: str,
    api_port: int,
    transport: httpx.BaseTransport | None = None,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> dict[str, object]:
    """GET one ModelConfig with the pod's projected ServiceAccount token over verified TLS."""

    try:
        token = (token_dir / "token").read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        raise ModelConfigError("modelconfig_credentials_unavailable", "Kubernetes API token is not mounted")
    ca = token_dir / "ca.crt"
    if transport is None and not ca.is_file():
        raise ModelConfigError("modelconfig_credentials_unavailable", "Kubernetes API CA bundle is not mounted")
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca)) if transport is None else True
    host = f"[{api_host}]" if ":" in api_host else api_host
    url = (f"https://{host}:{api_port}/apis/{API_GROUP_VERSION}/namespaces/"
           f"{quote(namespace, safe='')}/modelconfigs/{quote(name, safe='')}")
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}

    last = "Kubernetes API is unreachable"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(backoff_seconds * attempt)
        try:
            with httpx.Client(timeout=KUBE_API_TIMEOUT_SECONDS, verify=verify, transport=transport,
                              follow_redirects=False) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            last = f"Kubernetes API request failed ({type(exc).__name__})"
            continue
        if response.status_code == 200:
            if len(response.content) > MAX_MODELCONFIG_BYTES:
                raise ModelConfigError("modelconfig_invalid", "ModelConfig response exceeds its byte budget")
            try:
                document = response.json()
            except ValueError as exc:
                raise ModelConfigError("modelconfig_invalid", "ModelConfig response is not JSON") from exc
            if not isinstance(document, dict):
                raise ModelConfigError("modelconfig_invalid", "ModelConfig response is not an object")
            return document
        if response.status_code in (401, 403):
            raise ModelConfigError("modelconfig_forbidden",
                                   f"Kubernetes API returned HTTP {response.status_code} (RBAC or token)")
        if response.status_code == 404:
            raise ModelConfigError("modelconfig_not_found", f"ModelConfig {namespace}/{name} does not exist")
        if response.status_code < 500 and response.status_code != 429:
            raise ModelConfigError("modelconfig_unavailable", f"Kubernetes API returned HTTP {response.status_code}")
        last = f"Kubernetes API returned HTTP {response.status_code}"
    raise ModelConfigError("modelconfig_unavailable", last)


def resolve_from_env(
    environ: Mapping[str, str] = os.environ, *, transport: httpx.BaseTransport | None = None,
) -> ResolvedModelConfig:
    """Resolve the ModelConfig the pod was configured with; raise ModelConfigError otherwise."""

    name = environ.get("EWALLET_LLM_MODELCONFIG_NAME", "").strip()
    if not _NAME.fullmatch(name):
        raise ModelConfigError("modelconfig_misconfigured", "EWALLET_LLM_MODELCONFIG_NAME is missing or invalid")
    token_dir = Path(environ.get("EWALLET_KUBE_API_TOKEN_DIR", DEFAULT_TOKEN_DIR))
    namespace = environ.get("EWALLET_LLM_MODELCONFIG_NAMESPACE", "").strip()
    if not namespace:
        try:
            namespace = (token_dir / "namespace").read_text(encoding="utf-8").strip()
        except OSError:
            namespace = ""
    if not _NAME.fullmatch(namespace):
        raise ModelConfigError("modelconfig_misconfigured", "the ModelConfig namespace is unknown")
    secret_ref = _secret_ref(environ.get("EWALLET_LLM_API_KEY_SECRET_REF", "").strip())
    api_host = environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    try:
        api_port = int(environ.get("KUBERNETES_SERVICE_PORT", "443"))
    except ValueError:
        api_port = 0
    if not api_host or not 0 < api_port < 65536:
        raise ModelConfigError("modelconfig_misconfigured", "KUBERNETES_SERVICE_HOST/PORT are not set")
    document = fetch_model_config(name, namespace, token_dir=token_dir, api_host=api_host, api_port=api_port,
                                  transport=transport)
    return parse_model_config(document, name=name, namespace=namespace, secret_ref=secret_ref)


def log_resolution(resolved: ResolvedModelConfig) -> None:
    """One JSON line on stderr: agents resolve before their logging is configured."""

    if not _LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _LOG.addHandler(handler)
        _LOG.setLevel(logging.INFO)
        _LOG.propagate = False
    _LOG.info(json.dumps(resolved.audit(), sort_keys=True))
