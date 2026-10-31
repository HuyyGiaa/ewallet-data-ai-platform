"""FastAPI boundary for the Phase D MCP tool services.

Two services follow the release inventory: ``feature-rag-mcp`` serves
feature_lookup, fraud_scoring and knowledge_search; ``drift-mcp`` serves
drift_detection. Each exposes typed REST routes, a stateless MCP Streamable
HTTP endpoint (JSON responses, ``POST /mcp``) and live/ready probes.

Every /mcp and /v1/tools request passes ``RequestSecurityMiddleware`` first
(in-flight limit, Origin, body size, authentication) and is then authorized per
tool against the verified principal. Identity never comes from a client header;
the authentication mechanism itself is DECISION_REQUIRED (see ``security``).
"""

# No ``from __future__ import annotations``: the typed routes below are built
# at runtime and FastAPI must see the real Pydantic body classes.

import json
import logging
import os
import secrets
import sys
from typing import Any, Mapping
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from ai_platform.inference.errors import ConfigurationError
from ai_platform.llm_agent.contracts import (
    TOOL_NAMES,
    TRACEPARENT_RE,
    RequestContext,
    VersionContext,
)
from ai_platform.llm_agent.tools.runtime import HTTP_STATUS, ToolExecutor, ToolHandler
from ai_platform.llm_agent.tools.security import (
    REQUEST_ID_RE,
    AuthenticationError,
    Authenticator,
    DenyAllAuthenticator,
    HttpSecuritySettings,
    Principal,
    RequestSecurityMiddleware,
    ToolGrants,
    accepts_json,
    authenticator_readiness,
    error_response,
    is_json_content_type,
    verified_principal,
)

MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
SERVER_VERSION = "1.0.0"
DEFAULT_TIMEOUT_MS = 10_000
_JSONRPC_ID_TYPES = (str, int)
_VERSION_ENV = {
    "git_sha": "EWALLET_GIT_SHA",
    "image_digest": "EWALLET_IMAGE_DIGEST",
    "config_revision": "EWALLET_CONFIG_REVISION",
    "prompt_revision": "EWALLET_PROMPT_REVISION",
    "model_revision": "EWALLET_MODEL_REVISION",
    "feature_revision": "EWALLET_FEATURE_REVISION",
    "corpus_revision": "EWALLET_CORPUS_REVISION",
}


class BoundaryError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def versions_from_env(environment: Mapping[str, str] | None = None) -> VersionContext:
    """Deployment-supplied code/data identity; startup fails if any is missing."""

    values = os.environ if environment is None else environment
    try:
        return VersionContext(**{field: values[name].strip() for field, name in _VERSION_ENV.items()})
    except (KeyError, ValueError) as exc:
        raise ConfigurationError() from exc


def request_context(headers: Mapping[str, str], principal: Principal) -> RequestContext:
    # x-ewallet-actor is an assertion, never an identity: it must match the principal.
    asserted = headers.get("x-ewallet-actor")
    if asserted is not None and asserted != principal.subject:
        raise BoundaryError(403, "actor_mismatch", "The asserted actor does not match the authenticated principal")
    request_id = headers.get("x-request-id", "")
    if not REQUEST_ID_RE.fullmatch(request_id):
        request_id = str(uuid4())
    traceparent = headers.get("traceparent", "")
    if not TRACEPARENT_RE.fullmatch(traceparent):
        traceparent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    raw_timeout = headers.get("x-ewallet-timeout-ms", str(DEFAULT_TIMEOUT_MS))
    try:
        return RequestContext(
            request_id=request_id, traceparent=traceparent, actor_id=principal.subject,
            timeout_ms=int(raw_timeout), tool_call_budget=1,
        )
    except ValueError as exc:
        raise BoundaryError(400, "invalid_request_context", "Request context headers are invalid") from exc


def _error(status_code: int, request: Request, code: str, message: str) -> JSONResponse:
    return error_response(status_code, request.headers, code, message)


def _rpc_result(message_id: Any, result: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": message_id, "result": result})


def _rpc_error(message_id: Any, code: int, message: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


def _register_tool_route(app: FastAPI, executor: ToolExecutor, handler: ToolHandler, grants: ToolGrants) -> None:
    model = handler.input_model

    async def call_tool(payload: model, request: Request) -> JSONResponse:  # type: ignore[valid-type]
        principal = verified_principal(request.state)
        if not grants.allows(principal, handler.name):
            raise BoundaryError(403, "tool_forbidden", "The principal may not call this tool")
        context = request_context(request.headers, principal)
        response = await executor.call(handler.name, payload, context)
        return JSONResponse(status_code=HTTP_STATUS[response.status], content=response.to_dict())

    app.add_api_route(
        f"/v1/tools/{handler.name}", call_tool, methods=["POST"],
        name=handler.name, summary=handler.description,
    )


def create_tool_app(*, service_name: str, executor: ToolExecutor, authenticator: Authenticator,
                    grants: ToolGrants, http_settings: HttpSecuritySettings | None = None,
                    logger: logging.Logger | None = None) -> FastAPI:
    # No unauthenticated schema endpoints: /docs, /redoc and /openapi.json are off.
    app = FastAPI(title=f"E-Wallet {service_name}", version=SERVER_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.executor = executor
    app.add_middleware(RequestSecurityMiddleware, settings=http_settings or HttpSecuritySettings(),
                       authenticator=authenticator, logger=logger)

    @app.exception_handler(BoundaryError)
    async def boundary_error(request: Request, exc: BoundaryError) -> JSONResponse:
        return _error(exc.status_code, request, exc.code, exc.message)

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request: Request, exc: AuthenticationError) -> JSONResponse:
        return _error(exc.status_code, request, exc.code, "Authentication is required")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only; rejected input values are never echoed.
        locations = "; ".join(".".join(str(part) for part in item["loc"]) for item in exc.errors())
        return _error(422, request, "invalid_arguments",
                      f"Tool arguments failed validation at: {locations}"[:512])

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        return _error(500, request, "internal_error", "Internal tool error")

    for handler in executor.handlers.values():
        _register_tool_route(app, executor, handler, grants)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        # Transport readiness only: can this pod authenticate and serve protected
        # requests? Tool backends (Feast, KServe, drift baselines) never gate it;
        # their tools fail closed per call and are reported by /v1/capabilities.
        reason = await authenticator_readiness(authenticator)
        body = {"status": "ready" if reason is None else "not_ready", "service": service_name, "scope": "transport"}
        if reason is not None:
            body["reason"] = reason
        return JSONResponse(status_code=200 if reason is None else 503, content=body)

    @app.get("/v1/capabilities")
    async def capabilities(request: Request) -> JSONResponse:
        # Authenticated, and limited to the caller's granted tools.
        principal = verified_principal(request.state)
        report = await executor.readiness()
        return JSONResponse({"service": service_name, "tools": {
            name: {"status": item["status"], "code": item.get("code")}
            for name, item in report.items() if grants.allows(principal, name)
        }})

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        # Streamable HTTP, 2025-06-18: JSON responses only, no sessions, no batches.
        principal = verified_principal(request.state)
        if not accepts_json(request.headers.get("accept")):
            return _error(406, request, "not_acceptable", "The client must accept application/json")
        if not is_json_content_type(request.headers.get("content-type")):
            return _error(415, request, "unsupported_media_type", "Content-Type must be application/json")
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in MCP_PROTOCOL_VERSIONS:
            return _rpc_error(None, -32600, "Unsupported MCP protocol version", status_code=400)
        try:
            message = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return _rpc_error(None, -32700, "Parse error", status_code=400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid request", status_code=400)
        if "method" not in message:
            # A client's JSON-RPC response (e.g. to a server request) is acknowledged only.
            if "id" in message and ("result" in message) != ("error" in message):
                return Response(status_code=202)
            return _rpc_error(None, -32600, "Invalid request", status_code=400)
        if not isinstance(message["method"], str):
            return _rpc_error(None, -32600, "Invalid request", status_code=400)
        if "id" not in message:
            return Response(status_code=202)
        message_id, method = message["id"], message["method"]
        if isinstance(message_id, bool) or not isinstance(message_id, _JSONRPC_ID_TYPES):
            return _rpc_error(None, -32600, "Invalid request id", status_code=400)
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _rpc_error(message_id, -32602, "Invalid params")
        if method == "initialize":
            requested = params.get("protocolVersion")
            return _rpc_result(message_id, {
                "protocolVersion": requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": service_name, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _rpc_result(message_id, {})
        if method == "tools/list":
            return _rpc_result(message_id, {"tools": [
                {"name": handler.name, "description": handler.description,
                 "inputSchema": handler.input_model.model_json_schema()}
                for handler in executor.handlers.values() if grants.allows(principal, handler.name)
            ]})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            # An ungranted tool is indistinguishable from an unknown one (no enumeration).
            if (not isinstance(name, str) or name not in executor.handlers or name not in TOOL_NAMES
                    or not grants.allows(principal, name)):
                return _rpc_error(message_id, -32602, "Unknown tool")
            if not isinstance(arguments, dict):
                return _rpc_error(message_id, -32602, "Invalid params")
            context = request_context(request.headers, principal)
            response = await executor.call(name, arguments, context)
            payload = response.to_dict()
            return _rpc_result(message_id, {
                "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True, default=str)}],
                "structuredContent": payload,
                "isError": response.status != "ok",
            })
        return _rpc_error(message_id, -32601, "Method not found")

    return app


def audit_logger() -> logging.Logger:
    """JSON-lines tool-call events on stdout; uvicorn configures only its own loggers."""

    logger = logging.getLogger("ewallet.llm_agent.tools")
    if not any(getattr(handler, "_ewallet_audit", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._ewallet_audit = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _executor(handlers: tuple[ToolHandler, ...], environment: Mapping[str, str]) -> ToolExecutor:
    return ToolExecutor(
        handlers,
        versions=versions_from_env(environment),
        max_concurrency=int(environment.get("EWALLET_TOOL_MAX_CONCURRENCY", "8")),
        max_timeout_ms=int(environment.get("EWALLET_TOOL_MAX_TIMEOUT_MS", "30000")),
        logger=audit_logger(),
    )


def _production_app(service_name: str, handlers: tuple[ToolHandler, ...], environment: Mapping[str, str]) -> FastAPI:
    # DECISION_REQUIRED: no authentication mechanism is approved yet, so the
    # production servers deny every tool request (fail closed) until one is wired.
    return create_tool_app(
        service_name=service_name,
        executor=_executor(handlers, environment),
        authenticator=DenyAllAuthenticator(),
        grants=ToolGrants.from_env(environment),
        http_settings=HttpSecuritySettings.from_env(environment),
        logger=audit_logger(),
    )


def create_feature_rag_app(environment: Mapping[str, str] | None = None) -> FastAPI:
    """Production wiring: Feast Online + KServe + fail-closed knowledge retrieval."""

    from pathlib import Path

    from feast import FeatureStore

    from ai_platform.inference.assembler import CanonicalFeatureAssembler
    from ai_platform.inference.config import InferenceSettings, configure_feast_redis_timeouts
    from ai_platform.inference.feast_online import FeastOnlineAdapter
    from ai_platform.inference.kserve import KServeV2Client
    from ai_platform.inference.observability import default_telemetry
    from ai_platform.inference.service import InferenceService
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
    from ai_platform.llm_agent.tools.feature_lookup import FeatureLookupTool
    from ai_platform.llm_agent.tools.fraud_scoring import FraudScoringTool
    from ai_platform.llm_agent.tools.knowledge_search import KnowledgeSearchTool, UnavailableKnowledgeRetriever

    values = os.environ if environment is None else environment
    settings = InferenceSettings.from_env(values)
    store = FeatureStore(repo_path=str(settings.feast_repo_path))
    configure_feast_redis_timeouts(store, timeout_seconds=settings.feast_timeout_seconds)
    features = FeastOnlineAdapter(store, timeout_seconds=settings.feast_timeout_seconds)
    scoring = InferenceService(
        feature_provider=features,
        assembler=CanonicalFeatureAssembler(),
        predictor=KServeV2Client(
            base_url=settings.kserve_base_url, model_name=settings.model_name,
            model_version=settings.model_version, timeout_seconds=settings.kserve_timeout_seconds,
        ),
        telemetry=default_telemetry(),
    )
    index = TrustedChunkIndex.from_manifest(Path(values["EWALLET_RAG_MANIFEST"]))
    handlers = (
        FeatureLookupTool(features),
        FraudScoringTool(scoring, model_name=settings.model_name, model_version=settings.model_version),
        KnowledgeSearchTool(_knowledge_retriever(index, store, settings.feast_timeout_seconds, values)),
    )
    return _production_app("feature-rag-mcp", handlers, values)


def _knowledge_retriever(index, store, timeout_seconds: float, values: Mapping[str, str]):
    """Exact retrieval over Feast Online rows only when explicitly configured.

    EWALLET_RAG_RETRIEVER=feast-online-exact needs EWALLET_RAG_EMBEDDING_ENDPOINT
    (OpenAI-compatible, serving the manifest's embedding model revision);
    anything else keeps the fail-closed placeholder.
    """

    from ai_platform.llm_agent.rag.online_retriever import (
        FeastChunkRowSource,
        FeastOnlineExactRetriever,
        OpenAICompatibleQueryEmbedder,
    )
    from ai_platform.llm_agent.tools.knowledge_search import UnavailableKnowledgeRetriever

    mode = values.get("EWALLET_RAG_RETRIEVER", "unavailable")
    if mode == "unavailable":
        return UnavailableKnowledgeRetriever(index, online_store=values.get("EWALLET_RAG_ONLINE_STORE", "redis"))
    if mode != "feast-online-exact" or not values.get("EWALLET_RAG_EMBEDDING_ENDPOINT"):
        raise ConfigurationError()
    manifest = index.manifest
    embedder = OpenAICompatibleQueryEmbedder(
        endpoint=values["EWALLET_RAG_EMBEDDING_ENDPOINT"], model=manifest.embedding_model_id,
        model_revision=manifest.embedding_model_revision, dimensions=manifest.embedding_dimensions,
    )
    return FeastOnlineExactRetriever(index, FeastChunkRowSource(store, timeout_seconds=timeout_seconds), embedder)


def create_drift_app(environment: Mapping[str, str] | None = None) -> FastAPI:
    """Production wiring: no approved baseline registry or window source yet."""

    from ai_platform.llm_agent.tools.drift import (
        DriftDetectionTool,
        UnavailableBaselineStore,
        UnavailableWindowSampler,
    )

    values = os.environ if environment is None else environment
    handlers = (DriftDetectionTool(UnavailableBaselineStore(), UnavailableWindowSampler()),)
    return _production_app("drift-mcp", handlers, values)
