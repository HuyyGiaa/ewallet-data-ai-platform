"""The one approved Gateway environment of an Agent process (CI/CD V2-P1).

Every Gateway address an Agent may use (A2A Gateway, MCP Gateway, LLM agentgateway) and every address it
advertises (Agent Card, ``x-kagent-host``) is derived here from one namespace that must be on a fixed,
compiled-in allowlist. Nothing outside the allowlist can be configured: no hostname, IP, port, path or URL
is read from the environment, only the namespace selector ``EWALLET_GATEWAY_NAMESPACE``.

    unset                   -> ewallet-agents (production, the pre-V2 behaviour)
    ewallet-agents          -> production
    ewallet-agents-staging  -> staging
    anything else           -> GatewayEnvironmentError

Staging never falls back to production. ``EWALLET_RUNTIME_NAMESPACE`` (the pod's own namespace, rendered by
the staging chart profile from the downward API) is a second signal: a process running in a ``*-staging``
namespace must select the staging Gateway explicitly, so a forgotten selector is an error, not a silent
production fallback. Unset (production, CI, a developer shell), the selector alone decides.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Mapping
from urllib.parse import urlparse

GATEWAY_NAMESPACE_ENV = "EWALLET_GATEWAY_NAMESPACE"
RUNTIME_NAMESPACE_ENV = "EWALLET_RUNTIME_NAMESPACE"

PRODUCTION_GATEWAY_NAMESPACE = "ewallet-agents"
STAGING_GATEWAY_NAMESPACE = "ewallet-agents-staging"
APPROVED_GATEWAY_NAMESPACES = frozenset({PRODUCTION_GATEWAY_NAMESPACE, STAGING_GATEWAY_NAMESPACE})
# The SandboxAgent namespace that belongs to each Gateway namespace.
SANDBOX_NAMESPACES = {PRODUCTION_GATEWAY_NAMESPACE: "ewallet-sandbox",
                      STAGING_GATEWAY_NAMESPACE: "ewallet-sandbox-staging"}
STAGING_RUNTIME_NAMESPACES = frozenset({STAGING_GATEWAY_NAMESPACE, SANDBOX_NAMESPACES[STAGING_GATEWAY_NAMESPACE]})

A2A_GATEWAY_PORT = 8083
MCP_GATEWAY_PORT = 8080
LLM_GATEWAY_PORT = 3000
AGENT_PORT = 8080
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class GatewayEnvironmentError(ValueError):
    """The Gateway environment is not approved or is inconsistent with where the process runs."""


def is_staging_namespace(namespace: str) -> bool:
    """Any ``*-staging`` namespace is treated as staging, approved or not, so a mis-named one fails closed."""

    return namespace in STAGING_RUNTIME_NAMESPACES or namespace.endswith("-staging")


@dataclass(frozen=True)
class GatewayEnvironment:
    """The approved Gateway addresses of one environment. Construct only through the allowlist."""

    namespace: str = PRODUCTION_GATEWAY_NAMESPACE

    def __post_init__(self) -> None:
        if self.namespace not in APPROVED_GATEWAY_NAMESPACES:
            raise GatewayEnvironmentError(
                f"{GATEWAY_NAMESPACE_ENV} must be one of {sorted(APPROVED_GATEWAY_NAMESPACES)}")

    @property
    def is_staging(self) -> bool:
        return self.namespace == STAGING_GATEWAY_NAMESPACE

    @property
    def sandbox_namespace(self) -> str:
        return SANDBOX_NAMESPACES[self.namespace]

    @property
    def a2a_host(self) -> str:
        return f"a2a-gateway.{self.namespace}.svc.cluster.local"

    @property
    def a2a_endpoint(self) -> str:
        return f"http://{self.a2a_host}:{A2A_GATEWAY_PORT}/"

    @property
    def mcp_host(self) -> str:
        return f"mcp-gateway.{self.namespace}.svc"

    def mcp_route(self, path: str) -> str:
        if not re.fullmatch(r"/[a-z0-9-]+/mcp", path):
            raise GatewayEnvironmentError("MCP route must be /<server>/mcp")
        return f"http://{self.mcp_host}:{MCP_GATEWAY_PORT}{path}"

    @property
    def llm_host(self) -> str:
        return f"agentgateway.{self.namespace}.svc.cluster.local"

    @property
    def llm_base_url(self) -> str:
        return f"http://{self.llm_host}:{LLM_GATEWAY_PORT}/v1"

    def agent_host(self, agent: str) -> str:
        """``<agent>.<namespace>``: the A2A Gateway's ``x-kagent-host`` route key."""

        if not _DNS_LABEL.fullmatch(agent):
            raise GatewayEnvironmentError("agent name must be a DNS label")
        return f"{agent}.{self.namespace}"

    def agent_card_url(self, agent: str) -> str:
        return f"http://{self.agent_host(agent)}:{AGENT_PORT}"


PRODUCTION_GATEWAY = GatewayEnvironment(PRODUCTION_GATEWAY_NAMESPACE)
STAGING_GATEWAY = GatewayEnvironment(STAGING_GATEWAY_NAMESPACE)


def resolve_gateway_environment(environ: Mapping[str, str] | None = None) -> GatewayEnvironment:
    """Return the approved Gateway environment of this process or raise GatewayEnvironmentError."""

    environ = os.environ if environ is None else environ
    runtime = environ.get(RUNTIME_NAMESPACE_ENV)
    if runtime is not None and not _DNS_LABEL.fullmatch(runtime):
        raise GatewayEnvironmentError(f"{RUNTIME_NAMESPACE_ENV} must be a Kubernetes namespace name")
    selected = environ.get(GATEWAY_NAMESPACE_ENV)
    if selected is None:
        if runtime is not None and is_staging_namespace(runtime):
            raise GatewayEnvironmentError(
                f"runtime namespace {runtime} is staging: {GATEWAY_NAMESPACE_ENV}={STAGING_GATEWAY_NAMESPACE} "
                "is required (no production fallback)")
        return PRODUCTION_GATEWAY
    # Exact value only: no trimming, case folding or aliases.
    environment = GatewayEnvironment(selected)
    if runtime is not None:
        if environment.is_staging and runtime not in STAGING_RUNTIME_NAMESPACES:
            raise GatewayEnvironmentError(
                f"staging Gateway selected but the process runs in {runtime}, not a staging namespace")
        if not environment.is_staging and is_staging_namespace(runtime):
            raise GatewayEnvironmentError(
                f"production Gateway selected but the process runs in staging namespace {runtime}")
    return environment


def validate_service_url(value: str, *, host: str, port: int, path: str, what: str) -> None:
    """One exact ``http://<host>:<port><path>`` URL: no credentials, query, fragment, alias or encoding."""

    try:
        parsed = urlparse(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{what} is not a valid URL") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != host
        or parsed_port != port
        or parsed.path != path
        or parsed.netloc != f"{host}:{port}"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or "%" in value
        or value != value.strip()
    ):
        raise ValueError(f"{what} must be the approved in-cluster route http://{host}:{port}{path}")
