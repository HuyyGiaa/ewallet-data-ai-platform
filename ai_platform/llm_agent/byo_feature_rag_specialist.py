"""Deterministic kagent BYO Feature-RAG Specialist.

The Specialist accepts the canonical G3 A2A business request, calls only the
approved MCP Gateway with the official Streamable HTTP client, validates every
structured tool result and emits one Coordinator-compatible terminal artifact.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import math
from numbers import Integral, Real
import os
import re
from typing import Protocol
from urllib.parse import urlparse

import httpx2
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TaskState, TextPart
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from kagent.core.a2a import KAgentRequestContextBuilder
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_platform.llm_agent.business_request import G3BusinessRequest, business_request_from_a2a
from ai_platform.llm_agent.agent_metrics import (
    InflightTrackingExecutor,
    initialize as initialize_agent_metrics,
    record_invocation,
    record_mcp_retry,
    record_prompt_screen,
)
from ai_platform.llm_agent.mcp_failover import FailoverPolicy, is_recoverable, policy_from_env, with_failover
from ai_platform.llm_agent.byo_runtime import AppResources, audit, configure_telemetry, controller_task_store, flush_agent_metrics, listen_port
from ai_platform.llm_agent.nl_intake import (
    IntakeFailure,
    OpenAIToolPlanner,
    is_typed_wire_text,
    plan as plan_intake,
    planner_from_env,
    render_summary,
)
from ai_platform.llm_agent.contracts import Citation, VersionContext
from ai_platform.llm_agent.programmatic_coordinator import SpecialistEvidence


LOGGER = logging.getLogger(__name__)
APP_NAME = "feature-rag-specialist"
APP_VERSION = "1.0.0"
APP_URL = "http://feature-rag-specialist.ewallet-agents:8080"
APPROVED_MCP_GATEWAY = "http://mcp-gateway.ewallet-agents.svc:8080/feature-rag/mcp"
APPROVED_TOOLS = frozenset({"feature_lookup", "fraud_scoring", "knowledge_search"})
MAX_REQUEST_BYTES = 8_192
MAX_ERROR_CHARS = 512
MAX_MCP_TIMEOUT_SECONDS = 120.0
DEFAULT_TASK_TIMEOUT_SECONDS = 120.0
PROMPT_REVISION = "feature-rag-specialist/byo-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FeatureRagFailure(ValueError):
    """Stable fail-closed error without backend response leakage."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MCPEnvelope(BaseModel):
    """Exact shared tool response carried by MCP structuredContent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    tool_name: str
    status: str
    result: dict[str, object]
    versions: dict[str, str]
    citations: list[dict[str, object]] = Field(default_factory=list, max_length=8)
    fallback_used: bool = False


class MCPSession(Protocol):
    """The part of an MCP client session that the Specialist uses."""

    async def initialize(self): ...
    async def list_tools(self): ...
    async def call_tool(self, name: str, arguments: dict[str, object]): ...


SessionFactory = Callable[[str, Mapping[str, str], float], object]
TokenReader = Callable[[], str]


@asynccontextmanager
async def _official_session(
    endpoint: str,
    headers: Mapping[str, str],
    timeout: float,
) -> AsyncIterator[MCPSession]:
    async with httpx2.AsyncClient(headers=dict(headers), timeout=timeout) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                yield session


def _environment_token() -> str:
    return os.getenv("EWALLET_MCP_SERVICE_TOKEN", "").strip()


def _validate_endpoint(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "mcp-gateway.ewallet-agents.svc"
        or parsed.port != 8080
        or parsed.path != "/feature-rag/mcp"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("MCP endpoint must be the approved Feature-RAG Gateway route")


def _tool_arguments(request: G3BusinessRequest, tool: str) -> dict[str, object]:
    transaction = request.transaction.model_dump(mode="json", exclude_computed_fields=True)
    if tool == "feature_lookup":
        return {"transaction": transaction, "feature_names": list(request.feature_names)}
    if tool == "fraud_scoring":
        return {"transaction": transaction}
    if tool == "knowledge_search" and request.knowledge_query is not None:
        return {"query": request.knowledge_query, "top_k": 3}
    raise FeatureRagFailure("unsupported_tool", "Request contains an unsupported Feature-RAG tool")


def _parse_envelope(raw: object, *, request: G3BusinessRequest, tool: str) -> MCPEnvelope:
    if not isinstance(raw, Mapping):
        raise FeatureRagFailure("invalid_mcp_result", "MCP structured result is not an object")
    try:
        envelope = MCPEnvelope.model_validate(dict(raw))
        VersionContext(**envelope.versions)
        for item in envelope.citations:
            Citation(**item)
    except (ValidationError, TypeError, ValueError) as exc:
        raise FeatureRagFailure("invalid_mcp_result", "MCP structured result failed validation") from exc
    if (
        envelope.request_id != request.request_id
        or envelope.tool_name != tool
        or envelope.status != "ok"
        or envelope.fallback_used
    ):
        raise FeatureRagFailure("invalid_mcp_result", "MCP result identity or status mismatch")
    return envelope


def _validated_features(envelope: MCPEnvelope, request: G3BusinessRequest) -> dict[str, object]:
    result = envelope.result
    if result.get("transaction_id") != request.transaction.transaction_id:
        raise FeatureRagFailure("invalid_mcp_result", "Feature result transaction mismatch")
    features = result.get("historical_features")
    if not isinstance(features, Mapping) or set(features) != set(request.feature_names):
        raise FeatureRagFailure("invalid_mcp_result", "Feature result projection mismatch")
    validated: dict[str, object] = {}
    for name, item in features.items():
        if not isinstance(item, Mapping) or set(item) != {"value", "event_timestamp"}:
            raise FeatureRagFailure("invalid_mcp_result", "Feature result shape mismatch")
        value, timestamp = item["value"], item["event_timestamp"]
        if isinstance(value, bool) or not (
            value is None or isinstance(value, Integral) or isinstance(value, Real) and math.isfinite(float(value))
        ):
            raise FeatureRagFailure("invalid_mcp_result", "Feature value is not finite JSON data")
        if timestamp is not None and not isinstance(timestamp, str):
            raise FeatureRagFailure("invalid_mcp_result", "Feature timestamp is invalid")
        validated[str(name)] = dict(item)
    return validated


def _validated_score(envelope: MCPEnvelope, request: G3BusinessRequest) -> dict[str, object]:
    result = envelope.result
    required = {
        "transaction_id", "fraud_probability", "fraud_prediction", "model_name",
        "model_version", "feature_timestamp", "model_input_schema_sha256",
    }
    if set(result) != required or result.get("transaction_id") != request.transaction.transaction_id:
        raise FeatureRagFailure("invalid_mcp_result", "Fraud result identity or shape mismatch")
    probability, prediction = result["fraud_probability"], result["fraud_prediction"]
    if (
        isinstance(probability, bool)
        or not isinstance(probability, Real)
        or not math.isfinite(float(probability))
        or not 0.0 <= float(probability) <= 1.0
        or isinstance(prediction, bool)
        or not isinstance(prediction, Integral)
        or int(prediction) not in {0, 1}
    ):
        raise FeatureRagFailure("invalid_mcp_result", "Fraud prediction is invalid")
    if not all(isinstance(result[name], str) and result[name] for name in (
        "model_name", "model_version", "feature_timestamp",
    )) or not isinstance(result["model_input_schema_sha256"], str) or not _SHA256.fullmatch(
        result["model_input_schema_sha256"]
    ):
        raise FeatureRagFailure("invalid_mcp_result", "Fraud provenance is invalid")
    return dict(result)


def _validated_citations(envelope: MCPEnvelope, request: G3BusinessRequest) -> list[dict[str, object]]:
    result = envelope.result
    if request.knowledge_query is None or result.get("query_sha256") != hashlib.sha256(
        request.knowledge_query.encode("utf-8")
    ).hexdigest():
        raise FeatureRagFailure("invalid_mcp_result", "Knowledge query identity mismatch")
    hits = result.get("results")
    if not isinstance(hits, list) or not hits or not envelope.citations:
        raise FeatureRagFailure("invalid_mcp_result", "Knowledge result has no cited evidence")
    hit_keys = {
        (item.get("document_id"), item.get("chunk_id"), item.get("score"))
        for item in hits if isinstance(item, Mapping)
    }
    citation_keys = {
        (item["document_id"], item["chunk_id"], item["score"])
        for item in envelope.citations
    }
    if hit_keys != citation_keys or len(hit_keys) != len(hits):
        raise FeatureRagFailure("invalid_mcp_result", "Knowledge results and citations disagree")
    return [dict(item) for item in envelope.citations]


class FeatureRagMCPClient:
    """Bounded official MCP client for the one approved Specialist route."""

    def __init__(
        self,
        *,
        endpoint: str = APPROVED_MCP_GATEWAY,
        timeout_seconds: float = 30.0,
        token_reader: TokenReader = _environment_token,
        session_factory: SessionFactory = _official_session,
        failover: FailoverPolicy = FailoverPolicy(),
    ) -> None:
        _validate_endpoint(endpoint)
        if not 0.1 <= timeout_seconds <= MAX_MCP_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._token_reader = token_reader
        self._session_factory = session_factory
        self._failover = failover

    async def run(self, request: G3BusinessRequest) -> SpecialistEvidence:
        """Call the approved tools, failing over between replicas when that is safe.

        The session is reopened for each attempt. MCP servers are stateless and every
        approved tool is read-only, so a new attempt can land on a healthy replica.
        One overall deadline covers all attempts.
        """

        try:
            async with asyncio.timeout(self._timeout):
                return await with_failover(
                    lambda: self._run_once(request),
                    tools=request.requested_tools,
                    is_transient=lambda error: isinstance(error, FeatureRagFailure)
                    and error.code == "mcp_transport_error"
                    and is_recoverable(error),
                    policy=self._failover,
                    on_retry=lambda attempt: record_mcp_retry(APP_NAME),
                )
        except TimeoutError as exc:
            raise FeatureRagFailure("mcp_timeout", "MCP execution exceeded its deadline") from exc

    async def _run_once(self, request: G3BusinessRequest) -> SpecialistEvidence:
        if request.target_agent != APP_NAME or request.capabilities != ("fraud-evidence",):
            raise FeatureRagFailure("invalid_business_request", "Request is not for Feature-RAG")
        token = self._token_reader().strip()
        if not token:
            raise FeatureRagFailure("mcp_credential_unavailable", "Specialist MCP credential is unavailable")
        headers = {
            "X-Ewallet-MCP-Service-Token": token,
            "X-Request-Id": request.request_id,
        }
        propagate.inject(headers)
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session_factory(self._endpoint, headers, self._timeout) as session:
                    await session.initialize()
                    discovered = await session.list_tools()
                    names = {item.name for item in discovered.tools}
                    if names != APPROVED_TOOLS or not set(request.requested_tools) <= names:
                        raise FeatureRagFailure("mcp_tool_missing", "MCP tool inventory is not the approved allowlist")
                    features: dict[str, object] = {}
                    fraud_score: dict[str, object] | None = None
                    citations: list[dict[str, object]] = []
                    for tool in request.requested_tools:
                        response = await session.call_tool(tool, _tool_arguments(request, tool))
                        if response.is_error:
                            raise FeatureRagFailure("mcp_tool_failed", "MCP tool returned an error")
                        envelope = _parse_envelope(
                            response.structured_content,
                            request=request,
                            tool=tool,
                        )
                        if tool == "feature_lookup":
                            features = _validated_features(envelope, request)
                        elif tool == "fraud_scoring":
                            fraud_score = _validated_score(envelope, request)
                        elif tool == "knowledge_search":
                            citations = _validated_citations(envelope, request)
        except TimeoutError as exc:
            raise FeatureRagFailure("mcp_timeout", "MCP execution exceeded its deadline") from exc
        except FeatureRagFailure:
            raise
        except Exception as exc:
            raise FeatureRagFailure("mcp_transport_error", "MCP Gateway session failed") from exc

        return SpecialistEvidence(
            status="ok",
            request_id=request.request_id,
            features=features,
            fraud_score=fraud_score,
            drift=[],
            citations=citations,
            unavailable=[],
            contract_version="g3-orchestration/v1",
            prompt_revision=PROMPT_REVISION,
        )


def _safe_error(code: str, message: str) -> str:
    return json.dumps({
        "status": "error",
        "code": code[:64],
        "message": message[:MAX_ERROR_CHARS],
        "specialist_revision": PROMPT_REVISION,
    }, sort_keys=True, separators=(",", ":"))


def _on_screen(result, task_id: str | None) -> None:
    record_prompt_screen(APP_NAME, result)
    if result.detected:
        audit("specialist.prompt_pii_masked", task_id=task_id, mode="natural-language",
              pii_types=",".join(sorted(name for name, count in result.counts.items() if count)))


def _agent_message(text: str):
    from a2a.utils import new_agent_text_message
    return new_agent_text_message(text)


_UNSET = object()
_INTAKE_REJECTIONS = frozenset({
    "invalid_prompt", "missing_transaction_identifiers", "unsupported_request",
    "ambiguous_identifier", "missing_or_ambiguous_field", "ungrounded_argument",
    "invalid_tool_arguments", "unauthorized_tool", "multiple_tool_calls",
})


class FeatureRagSpecialistExecutor(AgentExecutor):
    """Official A2A lifecycle adapter around the deterministic MCP client."""

    def __init__(
        self,
        *,
        mcp_client: FeatureRagMCPClient | None = None,
        timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
        planner: OpenAIToolPlanner | None | object = _UNSET,
    ) -> None:
        if not 0.01 <= timeout_seconds <= MAX_MCP_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0.01 and 120")
        self._client = mcp_client or FeatureRagMCPClient(
            timeout_seconds=min(timeout_seconds, 30.0), failover=policy_from_env(os.environ)
        )
        # kagent Chat sends a random messageId, so a typed request can never
        # satisfy id == messageId == request_id from the UI. The bounded intake
        # builds the typed request server-side instead; the typed contract is
        # unchanged for the Coordinator.
        self._planner = planner_from_env() if planner is _UNSET else planner
        self._timeout = timeout_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._completed: dict[tuple[str, str], tuple[str, SpecialistEvidence]] = {}
        self._lock = asyncio.Lock()
        self._tracer = trace.get_tracer(__name__)

    @staticmethod
    def _fingerprint(request: G3BusinessRequest) -> str:
        payload = request.model_dump_json(exclude_none=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _run_idempotently(
        self,
        *,
        context_id: str,
        request: G3BusinessRequest,
    ) -> SpecialistEvidence:
        key = (context_id, request.request_id)
        fingerprint = self._fingerprint(request)
        async with self._lock:
            cached = self._completed.get(key)
        if cached is not None:
            cached_fingerprint, evidence = cached
            if cached_fingerprint != fingerprint:
                raise FeatureRagFailure(
                    "idempotency_conflict",
                    "Request identity was reused with different business data",
                )
            return evidence

        evidence = await self._client.run(request)
        async with self._lock:
            cached = self._completed.get(key)
            if cached is None:
                self._completed[key] = (fingerprint, evidence)
                return evidence
            cached_fingerprint, cached_evidence = cached
        if cached_fingerprint != fingerprint:
            raise FeatureRagFailure(
                "idempotency_conflict",
                "Request identity was reused with different business data",
            )
        return cached_evidence

    @staticmethod
    def _single_text(context: RequestContext) -> str | None:
        message = context.message
        if message is None or len(message.parts) != 1:
            return None
        root = message.parts[0].root
        return root.text if isinstance(root, TextPart) else None

    @staticmethod
    def _payload(context: RequestContext) -> dict[str, object]:
        if context.message is None:
            raise FeatureRagFailure("invalid_business_request", "A2A message is required")
        return {
            "jsonrpc": "2.0",
            "id": context.message.message_id,
            "method": "message/send",
            "params": {
                "message": context.message.model_dump(mode="json", by_alias=True, exclude_none=True),
            },
        }

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A SDK must assign task_id and context_id before execution")
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        request_id: str | None = None
        terminal, mode = "failed", "typed"
        with self._tracer.start_as_current_span("g3.feature_rag_specialist.execute") as span:
            span.set_attribute("a2a.task_id", context.task_id)
            span.set_attribute("a2a.context_id", context.context_id)
            intake = None
            try:
                text = self._single_text(context)
                if text is not None and not is_typed_wire_text(text) and self._planner is not None:
                    mode = "natural-language"
                    await updater.update_status(TaskState.working, message=_agent_message(
                        "Đang dùng LLM (agentgateway → vLLM) để chọn công cụ và trích xuất tham số giao dịch…"))
                    planning = asyncio.create_task(plan_intake(
                        text, planner=self._planner, context_id=context.context_id,
                        message_id=context.message.message_id if context.message else "",
                        on_screen=lambda result: _on_screen(result, context.task_id),
                    ))
                    async with self._lock:
                        self._tasks[context.task_id] = planning
                    intake = await planning
                    request = intake.request
                    audit("specialist.llm_tool_selected", request_id=request.request_id, task_id=context.task_id,
                          mode="natural-language", tool=intake.selection.name,
                          prompt_tokens=intake.selection.prompt_tokens,
                          completion_tokens=intake.selection.completion_tokens)
                    await updater.update_status(TaskState.working, message=_agent_message(
                        f"LLM đã chọn công cụ `{intake.selection.name}`; đang gọi MCP qua MCP Gateway: "
                        + ", ".join(request.requested_tools) + "…"))
                else:
                    try:
                        request = business_request_from_a2a(self._payload(context))
                    except (ValueError, ValidationError) as exc:
                        raise FeatureRagFailure("invalid_business_request", str(exc)) from exc
                span.set_attribute("g3.request_id", request.request_id)
                request_id = request.request_id
                audit("specialist.task_started", request_id=request_id, task_id=context.task_id,
                      tool=",".join(request.requested_tools))
                task = asyncio.create_task(self._run_idempotently(
                    context_id=context.context_id,
                    request=request,
                ))
                async with self._lock:
                    self._tasks[context.task_id] = task
                evidence = await asyncio.wait_for(task, timeout=self._timeout)
                if intake is not None:
                    await updater.add_artifact(
                        [Part(root=TextPart(text=render_summary(
                            evidence.model_dump(mode="json"), request, intake.selection)))],
                        artifact_id=f"answer-{context.task_id}",
                        name="analyst-answer",
                        last_chunk=True,
                    )
                artifact = evidence.model_dump_json(exclude_none=True)
                await updater.add_artifact(
                    [Part(root=TextPart(text=artifact))],
                    artifact_id=f"result-{context.task_id}",
                    name="validated-feature-rag-evidence",
                    last_chunk=True,
                )
                await updater.complete()
                terminal = "completed"
                span.set_attribute("g3.status", "completed")
                audit("specialist.task_completed", request_id=request_id, task_id=context.task_id,
                      status=evidence.status)
            except asyncio.TimeoutError:
                span.set_attribute("g3.status", "timeout")
                audit("specialist.task_failed", request_id=request_id, task_id=context.task_id, code="timeout")
                await updater.failed(message=_agent_message(_safe_error("timeout", "Specialist deadline exceeded")))
            except asyncio.CancelledError:
                span.set_attribute("g3.status", "cancelled")
                terminal = "cancelled"
                return
            except IntakeFailure as exc:
                span.set_attribute("g3.status", exc.code)
                audit("specialist.intake_rejected" if exc.code in _INTAKE_REJECTIONS else "specialist.task_failed",
                      request_id=request_id, task_id=context.task_id, mode="natural-language", code=exc.code)
                message = _agent_message(
                    f"Yêu cầu không được thực thi [{exc.code}]: {str(exc)[:MAX_ERROR_CHARS]}\n\n"
                    + _safe_error(exc.code, str(exc))
                )
                if exc.code in _INTAKE_REJECTIONS:
                    await updater.reject(message=message)
                    terminal = "rejected"
                else:
                    await updater.failed(message=message)
            except FeatureRagFailure as exc:
                span.set_attribute("g3.status", exc.code)
                audit("specialist.task_rejected" if exc.code in {"invalid_business_request", "idempotency_conflict"}
                      else "specialist.task_failed", request_id=request_id, task_id=context.task_id, code=exc.code)
                message = _agent_message(_safe_error(exc.code, str(exc)))
                if exc.code in {"invalid_business_request", "idempotency_conflict"}:
                    await updater.reject(message=message)
                    terminal = "rejected"
                else:
                    await updater.failed(message=message)
            except Exception:
                LOGGER.exception("BYO Feature-RAG Specialist failed", extra={"task_id": context.task_id})
                span.set_attribute("g3.status", "internal_error")
                audit("specialist.task_failed", request_id=request_id, task_id=context.task_id, code="internal_error")
                await updater.failed(message=_agent_message(_safe_error("internal_error", "Specialist failed closed")))
            finally:
                record_invocation(APP_NAME, mode, terminal)
                async with self._lock:
                    self._tasks.pop(context.task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("task_id and context_id are required for cancellation")
        async with self._lock:
            task = self._tasks.get(context.task_id)
        if task is not None:
            task.cancel()
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(message=_agent_message(_safe_error("cancelled", "Specialist task cancelled")))


def agent_card() -> AgentCard:
    """Return the Agent Card published at the well-known path."""

    return AgentCard(
        name="feature_rag_specialist",
        description="Fraud Investigation / Evidence specialist",
        version=APP_VERSION,
        url=APP_URL,
        preferredTransport="JSONRPC",
        protocolVersion="0.3",
        # kagent Chat uses message/stream; the Coordinator keeps message/send.
        capabilities=AgentCapabilities(streaming=True, stateTransitionHistory=True),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[AgentSkill(
            id="feature-rag-evidence",
            name="Feature, score and policy evidence",
            description="Grounded Feast, KServe and cited policy evidence for one transaction.",
            inputModes=["text"],
            outputModes=["text"],
            tags=["fraud", "features", "rag"],
        )],
    )


def build_app(
    *,
    task_store: TaskStore | None = None,
    executor: AgentExecutor | None = None,
) -> FastAPI:
    """Build the A2A application with its executor, task store and lifecycle."""

    configure_telemetry(APP_NAME, APP_VERSION)
    initialize_agent_metrics(APP_NAME)
    flush_agent_metrics()
    resources = AppResources(None, None)
    if task_store is None:
        task_store, resources = controller_task_store(APP_NAME)
    handler = DefaultRequestHandler(
        agent_executor=InflightTrackingExecutor(APP_NAME, executor or FeatureRagSpecialistExecutor()),
        task_store=task_store,
        request_context_builder=KAgentRequestContextBuilder(task_store=task_store),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if resources.controller_client is not None:
            await resources.controller_client.aclose()

    app = FastAPI(title="G3 BYO Feature-RAG Specialist", version=APP_VERSION, lifespan=lifespan)
    app.add_api_route("/health", lambda: PlainTextResponse("OK"), methods=["GET"])

    def ready() -> PlainTextResponse:
        if resources.token_hook is not None and not resources.token_hook.available():
            return PlainTextResponse("projected kagent token unavailable", status_code=503)
        if not _environment_token():
            return PlainTextResponse("Specialist MCP credential unavailable", status_code=503)
        return PlainTextResponse("READY")

    app.add_api_route("/ready", ready, methods=["GET"])
    A2AFastAPIApplication(
        agent_card=agent_card(),
        http_handler=handler,
        max_content_length=MAX_REQUEST_BYTES,
    ).add_routes_to_app(app)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = build_app()


def main() -> None:
    """Serve the Specialist on the configured port."""

    import uvicorn
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(app, host="0.0.0.0", port=listen_port(os.environ), access_log=True)


if __name__ == "__main__":
    main()
