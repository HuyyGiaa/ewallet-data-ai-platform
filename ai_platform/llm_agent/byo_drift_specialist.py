"""Deterministic kagent BYO Drift Specialist.

The Specialist accepts one canonical ``G3DriftRequest`` over A2A, calls
``drift_detection`` through the approved MCP Gateway route with the official
Streamable HTTP client, validates the result and emits one
Coordinator-compatible terminal artifact. It never calls an LLM: every drift
argument (feature, baseline revision, current window) arrives explicitly in the
typed request, and the PSI comes only from the drift MCP server.

A tool-level rejection or unavailability (unknown baseline, window outside the
approved observations, missing data source) is reported as structured
``unavailable`` evidence, never replaced by an invented result. Transport and
contract failures fail the task.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TextPart
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from kagent.core.a2a import KAgentRequestContextBuilder
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import ValidationError

from ai_platform.llm_agent.agent_metrics import (
    InflightTrackingExecutor,
    initialize as initialize_agent_metrics,
    record_invocation,
    record_mcp_retry,
)
from ai_platform.llm_agent.business_request import G3DriftRequest, drift_request_from_a2a
from ai_platform.llm_agent.byo_mcp import (
    MCPEnvelope,
    SessionFactory,
    TokenReader,
    environment_token,
    official_session,
    validate_gateway_route,
)
from ai_platform.llm_agent.byo_runtime import (
    AppResources,
    audit,
    configure_telemetry,
    controller_task_store,
    flush_agent_metrics,
    listen_port,
    shutdown_grace_seconds,
)
from ai_platform.llm_agent.contracts import VersionContext
from ai_platform.llm_agent.drift_evidence import DriftFinding
from ai_platform.llm_agent.mcp_failover import FailoverPolicy, is_recoverable, policy_from_env, with_failover
from ai_platform.llm_agent.programmatic_coordinator import SpecialistEvidence


LOGGER = logging.getLogger(__name__)
APP_NAME = "drift-specialist"
APP_VERSION = "1.1.0"
APP_URL = "http://drift-specialist.ewallet-agents:8080"
DRIFT_ROUTE = "/drift/mcp"
APPROVED_MCP_GATEWAY = f"http://mcp-gateway.ewallet-agents.svc:8080{DRIFT_ROUTE}"
APPROVED_TOOLS = frozenset({"drift_detection"})
MAX_REQUEST_BYTES = 8_192
MAX_ERROR_CHARS = 512
MAX_MCP_TIMEOUT_SECONDS = 120.0
DEFAULT_TASK_TIMEOUT_SECONDS = 60.0
PROMPT_REVISION = "drift-specialist/byo-v1"
_TOOL_FAILURE_STATUSES = frozenset({"rejected", "unavailable", "timeout"})


class DriftSpecialistFailure(ValueError):
    """Stable fail-closed error without backend response leakage."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evidence_from_envelope(raw: object, request: G3DriftRequest) -> SpecialistEvidence:
    """Validate one MCP structured result and convert it into Coordinator evidence."""

    if not isinstance(raw, dict):
        raise DriftSpecialistFailure("invalid_mcp_result", "MCP structured result is not an object")
    try:
        envelope = MCPEnvelope.model_validate(raw)
        VersionContext(**envelope.versions)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DriftSpecialistFailure("invalid_mcp_result", "MCP structured result failed validation") from exc
    if (
        envelope.request_id != request.request_id
        or envelope.tool_name != "drift_detection"
        or envelope.fallback_used
        or envelope.citations
    ):
        raise DriftSpecialistFailure("invalid_mcp_result", "MCP result identity mismatch")
    if envelope.status in _TOOL_FAILURE_STATUSES:
        error = envelope.result.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise DriftSpecialistFailure("invalid_mcp_result", "MCP tool failure has no error code")
        return SpecialistEvidence(
            status="unavailable", request_id=request.request_id, drift=[],
            unavailable=[{
                "tool": "drift_detection",
                "status": envelope.status,
                "code": str(error["code"])[:64],
                "message": str(error.get("message", ""))[:256],
                "retryable": bool(error.get("retryable", False)),
            }],
            contract_version="g3-orchestration/v1", prompt_revision=PROMPT_REVISION,
        )
    if envelope.status != "ok":
        raise DriftSpecialistFailure("mcp_tool_failed", "MCP drift tool returned an error")
    try:
        finding = DriftFinding.model_validate(envelope.result)
    except ValidationError as exc:
        raise DriftSpecialistFailure("invalid_mcp_result", "Drift result failed validation") from exc
    if not finding.matches(request):
        raise DriftSpecialistFailure("invalid_mcp_result", "Drift result answers another feature, baseline or window")
    item = finding.model_dump(mode="json", exclude_none=True)
    item["tool_versions"] = {
        "git_sha": envelope.versions["git_sha"],
        "image_digest": envelope.versions["image_digest"],
        "config_revision": envelope.versions["config_revision"],
    }
    return SpecialistEvidence(
        status="ok", request_id=request.request_id, drift=[item], unavailable=[],
        contract_version="g3-orchestration/v1", prompt_revision=PROMPT_REVISION,
    )


class DriftMCPClient:
    """Bounded official MCP client for the one approved drift route."""

    def __init__(
        self,
        *,
        endpoint: str = APPROVED_MCP_GATEWAY,
        timeout_seconds: float = 30.0,
        token_reader: TokenReader = environment_token,
        session_factory: SessionFactory = official_session,
        failover: FailoverPolicy = FailoverPolicy(),
    ) -> None:
        validate_gateway_route(endpoint, DRIFT_ROUTE)
        if not 0.1 <= timeout_seconds <= MAX_MCP_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._token_reader = token_reader
        self._session_factory = session_factory
        self._failover = failover
        self._tracer = trace.get_tracer(__name__)

    async def run(self, request: G3DriftRequest) -> SpecialistEvidence:
        try:
            async with asyncio.timeout(self._timeout):
                return await with_failover(
                    lambda: self._run_once(request),
                    tools=request.requested_tools,
                    is_transient=lambda error: isinstance(error, DriftSpecialistFailure)
                    and error.code == "mcp_transport_error"
                    and is_recoverable(error),
                    policy=self._failover,
                    on_retry=lambda attempt: record_mcp_retry(APP_NAME),
                )
        except TimeoutError as exc:
            raise DriftSpecialistFailure("mcp_timeout", "MCP execution exceeded its deadline") from exc

    async def _run_once(self, request: G3DriftRequest) -> SpecialistEvidence:
        token = self._token_reader().strip()
        if not token:
            raise DriftSpecialistFailure("mcp_credential_unavailable", "Specialist MCP credential is unavailable")
        with self._tracer.start_as_current_span("g3.drift_specialist.mcp_call") as span:
            span.set_attribute("g3.request_id", request.request_id)
            span.set_attribute("mcp.tool.name", "drift_detection")
            span.set_attribute("ewallet.gateway", "mcp-gateway")
            span.set_attribute("ewallet.mcp.route", DRIFT_ROUTE)
            headers = {"X-Ewallet-MCP-Service-Token": token, "X-Request-Id": request.request_id}
            propagate.inject(headers)
            try:
                async with self._session_factory(self._endpoint, headers, self._timeout) as session:
                    await session.initialize()
                    discovered = await session.list_tools()
                    if {item.name for item in discovered.tools} != APPROVED_TOOLS:
                        raise DriftSpecialistFailure("mcp_tool_missing", "MCP tool inventory is not the approved allowlist")
                    response = await session.call_tool("drift_detection", request.tool_arguments())
            except DriftSpecialistFailure:
                raise
            except TimeoutError as exc:
                raise DriftSpecialistFailure("mcp_timeout", "MCP execution exceeded its deadline") from exc
            except Exception as exc:
                span.set_attribute("ewallet.status", "mcp_transport_error")
                raise DriftSpecialistFailure("mcp_transport_error", "MCP Gateway session failed") from exc
            evidence = evidence_from_envelope(response.structured_content, request)
            span.set_attribute("ewallet.status", evidence.status)
            return evidence


def _safe_error(code: str, message: str) -> str:
    return json.dumps({
        "status": "error",
        "code": code[:64],
        "message": message[:MAX_ERROR_CHARS],
        "specialist_revision": PROMPT_REVISION,
    }, sort_keys=True, separators=(",", ":"))


def _agent_message(text: str):
    from a2a.utils import new_agent_text_message
    return new_agent_text_message(text)


_REJECTIONS = frozenset({"invalid_business_request", "idempotency_conflict"})


class DriftSpecialistExecutor(AgentExecutor):
    """Official A2A lifecycle adapter around the deterministic drift MCP client."""

    def __init__(
        self,
        *,
        mcp_client: DriftMCPClient | None = None,
        timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    ) -> None:
        if not 0.01 <= timeout_seconds <= MAX_MCP_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0.01 and 120")
        self._client = mcp_client or DriftMCPClient(
            timeout_seconds=min(timeout_seconds, 30.0), failover=policy_from_env(os.environ)
        )
        self._timeout = timeout_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._completed: dict[tuple[str, str], tuple[str, SpecialistEvidence]] = {}
        self._lock = asyncio.Lock()
        self._tracer = trace.get_tracer(__name__)

    async def _run_idempotently(self, *, context_id: str, request: G3DriftRequest) -> SpecialistEvidence:
        key = (context_id, request.request_id)
        fingerprint = hashlib.sha256(request.to_compact_json().encode("utf-8")).hexdigest()
        async with self._lock:
            cached = self._completed.get(key)
        if cached is None:
            evidence = await self._client.run(request)
            async with self._lock:
                cached = self._completed.setdefault(key, (fingerprint, evidence))
        if cached[0] != fingerprint:
            raise DriftSpecialistFailure("idempotency_conflict",
                                         "Request identity was reused with different business data")
        return cached[1]

    @staticmethod
    def _payload(context: RequestContext) -> dict[str, object]:
        if context.message is None:
            raise DriftSpecialistFailure("invalid_business_request", "A2A message is required")
        return {
            "jsonrpc": "2.0",
            "id": context.message.message_id,
            "method": "message/send",
            "params": {"message": context.message.model_dump(mode="json", by_alias=True, exclude_none=True)},
        }

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A SDK must assign task_id and context_id before execution")
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        request_id: str | None = None
        terminal = "failed"
        with self._tracer.start_as_current_span("g3.drift_specialist.execute") as span:
            span.set_attribute("a2a.task_id", context.task_id)
            span.set_attribute("a2a.context_id", context.context_id)
            span.set_attribute("ewallet.agent", APP_NAME)
            try:
                try:
                    request = drift_request_from_a2a(self._payload(context))
                except (ValueError, ValidationError) as exc:
                    raise DriftSpecialistFailure("invalid_business_request", str(exc)[:MAX_ERROR_CHARS]) from exc
                request_id = request.request_id
                span.set_attribute("g3.request_id", request_id)
                span.set_attribute("ewallet.drift.feature_name", request.feature_name)
                span.set_attribute("ewallet.drift.baseline_id", request.baseline_id)
                audit("specialist.task_started", request_id=request_id, task_id=context.task_id,
                      tool="drift_detection")
                task = asyncio.create_task(self._run_idempotently(context_id=context.context_id, request=request))
                async with self._lock:
                    self._tasks[context.task_id] = task
                evidence = await asyncio.wait_for(task, timeout=self._timeout)
                await updater.add_artifact(
                    [Part(root=TextPart(text=evidence.model_dump_json(exclude_none=True)))],
                    artifact_id=f"result-{context.task_id}",
                    name="validated-drift-evidence",
                    last_chunk=True,
                )
                await updater.complete()
                terminal = "completed"
                span.set_attribute("g3.status", evidence.status)
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
            except DriftSpecialistFailure as exc:
                span.set_attribute("g3.status", exc.code)
                rejected = exc.code in _REJECTIONS
                audit("specialist.task_rejected" if rejected else "specialist.task_failed",
                      request_id=request_id, task_id=context.task_id, code=exc.code)
                message = _agent_message(_safe_error(exc.code, str(exc)))
                if rejected:
                    await updater.reject(message=message)
                    terminal = "rejected"
                else:
                    await updater.failed(message=message)
            except Exception:
                LOGGER.exception("BYO Drift Specialist failed", extra={"task_id": context.task_id})
                span.set_attribute("g3.status", "internal_error")
                audit("specialist.task_failed", request_id=request_id, task_id=context.task_id, code="internal_error")
                await updater.failed(message=_agent_message(_safe_error("internal_error", "Specialist failed closed")))
            finally:
                record_invocation(APP_NAME, "typed", terminal)
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
    """Agent Card at the well-known path; the skill id is unchanged from the Declarative agent."""

    return AgentCard(
        name="drift_specialist",
        description="Feature drift specialist (PSI against a versioned baseline)",
        version=APP_VERSION,
        url=APP_URL,
        preferredTransport="JSONRPC",
        protocolVersion="0.3",
        capabilities=AgentCapabilities(streaming=True, stateTransitionHistory=True),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[AgentSkill(
            id="feature-risk-analysis",
            name="Feature drift analysis",
            description=(
                "PSI drift of one monitored feature over an explicit current window against an "
                "immutable baseline revision. Accepts only the typed CALL_AGENT=/REQUEST_JSON= protocol "
                "(g3-drift-request/v1)."
            ),
            inputModes=["text"],
            outputModes=["text"],
            tags=["fraud", "features", "drift"],
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
        agent_executor=InflightTrackingExecutor(APP_NAME, executor or DriftSpecialistExecutor()),
        task_store=task_store,
        request_context_builder=KAgentRequestContextBuilder(task_store=task_store),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if resources.controller_client is not None:
            await resources.controller_client.aclose()

    app = FastAPI(title="G3 BYO Drift Specialist", version=APP_VERSION, lifespan=lifespan)
    app.add_api_route("/health", lambda: PlainTextResponse("OK"), methods=["GET"])

    def ready() -> PlainTextResponse:
        if resources.token_hook is not None and not resources.token_hook.available():
            return PlainTextResponse("projected kagent token unavailable", status_code=503)
        if not environment_token():
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
    uvicorn.run(app, host="0.0.0.0", port=listen_port(os.environ), access_log=True,
                timeout_graceful_shutdown=shutdown_grace_seconds(os.environ))


if __name__ == "__main__":
    main()
