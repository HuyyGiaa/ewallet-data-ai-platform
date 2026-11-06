"""Official A2A server adapter for the deterministic G3 BYO Coordinator.

The adapter deliberately keeps orchestration in :mod:`programmatic_coordinator`.
It only translates A2A SDK request/event types, wires the kagent 0.10.3 task
store, exposes health endpoints and emits trace correlation attributes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from threading import Event, Lock
from typing import AsyncIterator, Callable

import httpx
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TaskState, TextPart
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from kagent.core.a2a import KAgentRequestContextBuilder
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from ai_platform.llm_agent.business_request import a2a_message_send
from ai_platform.llm_agent.agent_metrics import (
    InflightTrackingExecutor,
    initialize as initialize_agent_metrics,
    record_invocation,
    record_prompt_screen,
)
from ai_platform.llm_agent.byo_runtime import (
    AppResources,
    flush_agent_metrics,
    ControllerTokenHook,
    audit,
    configure_telemetry,
    controller_task_store,
)
from ai_platform.llm_agent.nl_intake import (
    IntakeFailure,
    OpenAIToolPlanner,
    is_typed_wire_text,
    plan as plan_intake,
    planner_from_env,
    render_summary,
)
from ai_platform.llm_agent.programmatic_coordinator import (
    CoordinatorFailure,
    GatewayCall,
    GatewayPolicy,
    ProgrammaticCoordinator,
)
from ai_platform.llm_agent.idempotency import IdempotencyScope
from ai_platform.llm_agent.redis_idempotency import idempotency_store_from_env


LOGGER = logging.getLogger(__name__)
APP_NAME = "fraud-coordinator"
APP_VERSION = "1.0.0"
APP_URL = "http://fraud-coordinator.ewallet-agents:8080"
MAX_REQUEST_BYTES = 8_192
MAX_ERROR_CHARS = 512
DEFAULT_TASK_TIMEOUT_SECONDS = 480.0
MAX_TASK_TIMEOUT_SECONDS = 600.0
# Input problems are rejections (the analyst can fix the question); LLM or
# gateway outages are failures. Neither path ever produces a prediction.
_INTAKE_REJECTIONS = frozenset({
    "invalid_prompt", "missing_transaction_identifiers", "unsupported_request",
    "ambiguous_identifier", "missing_or_ambiguous_field", "ungrounded_argument",
    "invalid_tool_arguments", "unauthorized_tool", "multiple_tool_calls",
    "nl_intake_disabled",
})
_UNSET = object()


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for the production Coordinator")
    return value


def _runtime_scope() -> IdempotencyScope:
    """Use only source-configured mesh identity, never a forwarded user header."""

    return IdempotencyScope(
        tenant=_required_environment("EWALLET_IDEMPOTENCY_TENANT"),
        principal=_required_environment("EWALLET_IDEMPOTENCY_PRINCIPAL"),
    )


def _safe_error(code: str, message: str) -> str:
    """Return a bounded, stable error artifact without backend response data."""

    return json.dumps(
        {
            "status": "error",
            "code": code[:64],
            "message": message[:MAX_ERROR_CHARS],
            "coordinator_revision": "programmatic-coordinator/byo-v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


class TracedGatewayTransport:
    """Bounded synchronous HTTP transport with W3C trace propagation."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(follow_redirects=False)

    def __call__(self, call: GatewayCall) -> dict[str, object]:
        headers = dict(call.headers)
        propagate.inject(headers)
        try:
            response = self._client.post(
                call.url,
                headers=headers,
                json=call.payload,
                timeout=call.timeout_seconds,
            )
            response.raise_for_status()
            raw = response.content
        except (httpx.HTTPError, TimeoutError) as exc:
            raise CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed") from exc
        if len(raw) > 32_768:
            raise CoordinatorFailure("response_too_large", "A2A response exceeds its byte budget")
        try:
            value = response.json()
        except ValueError as exc:
            raise CoordinatorFailure("invalid_a2a_response", "A2A Gateway returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CoordinatorFailure("invalid_a2a_response", "A2A Gateway response must be an object")
        return value


def deterministic_synthesis(_request: object, _evidence: object) -> str:
    """Provide bounded prose without adding any mutable business value."""

    return "Bằng chứng chuyên gia đã được xác thực; các giá trị nguồn được giữ nguyên bên dưới."


class CoordinatorExecutor(AgentExecutor):
    """Translate official A2A SDK lifecycle events to the deterministic core."""

    def __init__(
        self,
        coordinator_factory: Callable[[], ProgrammaticCoordinator] | None = None,
        timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
        idempotency_scope: IdempotencyScope | None = None,
        planner: OpenAIToolPlanner | None | object = _UNSET,
    ) -> None:
        if not 0.1 <= timeout_seconds <= MAX_TASK_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0.1 and 600")
        self._timeout_seconds = timeout_seconds
        if coordinator_factory is not None and idempotency_scope is None:
            raise ValueError("idempotency_scope is required with a custom Coordinator factory")
        self._scope = idempotency_scope
        self._coordinator_factory = coordinator_factory
        self._coordinator = coordinator_factory() if coordinator_factory is not None else None
        self._planner = planner_from_env() if planner is _UNSET else planner
        self._cancellations: dict[str, Event] = {}
        self._intake_tasks: dict[str, asyncio.Task] = {}
        self._lock = Lock()
        self._tracer = trace.get_tracer(__name__)

    def _ensure_runtime(self) -> tuple[ProgrammaticCoordinator, IdempotencyScope]:
        """Initialize production Redis wiring lazily, without a memory fallback."""

        with self._lock:
            if self._scope is None:
                self._scope = _runtime_scope()
            if self._coordinator is None:
                self._coordinator = self._default_coordinator()
            return self._coordinator, self._scope

    def _default_coordinator(self) -> ProgrammaticCoordinator:
        return ProgrammaticCoordinator(
            transport=TracedGatewayTransport(),
            synthesizer=deterministic_synthesis,
            idempotency_store=idempotency_store_from_env(),
            gateway=GatewayPolicy(timeout_seconds=self._timeout_seconds),
            lease_ms=int(os.getenv("EWALLET_IDEMPOTENCY_LEASE_MS", "600000")),
            completed_ttl_ms=int(os.getenv("EWALLET_IDEMPOTENCY_COMPLETED_TTL_MS", "86400000")),
            duplicate_wait_seconds=float(os.getenv("EWALLET_IDEMPOTENCY_WAIT_SECONDS", "15")),
            duplicate_poll_seconds=float(os.getenv("EWALLET_IDEMPOTENCY_POLL_SECONDS", "0.1")),
        )

    def ready(self) -> None:
        coordinator, _ = self._ensure_runtime()
        coordinator.ready()

    @staticmethod
    def _payload(context: RequestContext) -> dict[str, object]:
        message = context.message
        if message is None:
            raise CoordinatorFailure("invalid_business_request", "A2A message is required")
        return {
            "jsonrpc": "2.0",
            "id": message.message_id,
            "method": "message/send",
            "params": {"message": message.model_dump(mode="json", by_alias=True, exclude_none=True)},
        }

    @staticmethod
    def _single_text(context: RequestContext) -> str | None:
        message = context.message
        if message is None or len(message.parts) != 1:
            return None
        root = message.parts[0].root
        return root.text if isinstance(root, TextPart) else None

    async def _intake(self, context: RequestContext, text: str, updater: TaskUpdater):
        """Natural-language question -> LLM tool call -> grounded typed request."""

        if self._planner is None:
            raise IntakeFailure(
                "nl_intake_disabled",
                "Natural-language intake is disabled; send the typed CALL_AGENT=/REQUEST_JSON= protocol",
            )
        await updater.update_status(
            TaskState.working,
            message=_agent_message("Đang dùng LLM (agentgateway → vLLM) để chọn công cụ và trích xuất tham số giao dịch…"),
        )
        task = asyncio.create_task(plan_intake(
            text,
            planner=self._planner,
            context_id=context.context_id or "",
            message_id=context.message.message_id if context.message else "",
            on_screen=lambda result: _on_screen(result, context.task_id),
        ))
        with self._lock:
            self._intake_tasks[context.task_id or ""] = task
        try:
            intake = await task
        finally:
            with self._lock:
                self._intake_tasks.pop(context.task_id or "", None)
        audit(
            "coordinator.llm_tool_selected",
            request_id=intake.request.request_id, task_id=context.task_id, mode="natural-language",
            tool=intake.selection.name, target_agent=intake.request.target_agent,
            prompt_tokens=intake.selection.prompt_tokens, completion_tokens=intake.selection.completion_tokens,
        )
        await updater.update_status(
            TaskState.working,
            message=_agent_message(
                f"LLM đã chọn công cụ `{intake.selection.name}`; đang ủy quyền cho "
                f"{intake.request.target_agent} qua A2A Gateway (request_id {intake.request.request_id})…"
            ),
        )
        return intake

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A SDK must assign task_id and context_id before execution")
        task_id, context_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)
        coordinator, scope = self._ensure_runtime()
        cancellation = Event()
        with self._lock:
            self._cancellations[task_id] = cancellation
        await updater.start_work()

        message_id = context.message.message_id if context.message else "missing"
        text = self._single_text(context)
        natural_language = text is not None and not is_typed_wire_text(text)
        mode = "natural-language" if natural_language else "typed"
        started = asyncio.get_running_loop().time()
        with self._tracer.start_as_current_span("g3.coordinator.execute") as span:
            span.set_attribute("g3.request_id", message_id)
            span.set_attribute("g3.intake_mode", mode)
            span.set_attribute("a2a.task_id", task_id)
            span.set_attribute("a2a.context_id", context_id)
            audit("coordinator.task_started", task_id=task_id, context_id=context_id, mode=mode)
            request_id = message_id
            terminal = "failed"
            try:
                intake = None
                if natural_language:
                    intake = await self._intake(context, text or "", updater)
                    request_id = intake.request.request_id
                    span.set_attribute("g3.request_id", request_id)
                    payload: dict[str, object] = a2a_message_send(intake.request)
                else:
                    payload = self._payload(context)
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        coordinator.handle,
                        payload,
                        scope=scope,
                        session_id=context_id,
                        cancelled=cancellation,
                    ),
                    timeout=self._timeout_seconds + 1.0,
                )
                if cancellation.is_set():
                    terminal = "cancelled"
                    return
                result["coordinator_revision"] = "programmatic-coordinator/byo-v1"
                if intake is not None:
                    await updater.add_artifact(
                        [Part(root=TextPart(text=render_summary(result, intake.request, intake.selection)))],
                        artifact_id=f"answer-{task_id}",
                        name="analyst-answer",
                        last_chunk=True,
                    )
                artifact = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                await updater.add_artifact(
                    [Part(root=TextPart(text=artifact))],
                    artifact_id=f"result-{task_id}",
                    name="validated-fraud-investigation",
                    last_chunk=True,
                )
                await updater.complete()
                terminal = "completed"
                span.set_attribute("g3.status", "completed")
                audit("coordinator.task_completed", request_id=request_id, task_id=task_id, mode=mode,
                      status=str(result.get("status")), duration_ms=self._elapsed_ms(started))
            except asyncio.CancelledError:
                terminal = "cancelled"
                span.set_attribute("g3.status", "cancelled")
                audit("coordinator.task_cancelled", request_id=request_id, task_id=task_id, mode=mode)
                return
            except asyncio.TimeoutError:
                cancellation.set()
                span.set_attribute("g3.status", "timeout")
                audit("coordinator.task_failed", request_id=request_id, task_id=task_id, mode=mode, code="timeout")
                await updater.failed(message=_agent_message(_safe_error("timeout", "Coordinator deadline exceeded")))
            except IntakeFailure as exc:
                span.set_attribute("g3.status", exc.code)
                audit("coordinator.intake_rejected" if exc.code in _INTAKE_REJECTIONS else "coordinator.task_failed",
                      request_id=request_id, task_id=task_id, mode=mode, code=exc.code)
                message = _agent_message(
                    f"Yêu cầu không được thực thi [{exc.code}]: {str(exc)[:MAX_ERROR_CHARS]}\n\n"
                    + _safe_error(exc.code, str(exc))
                )
                if exc.code in _INTAKE_REJECTIONS:
                    await updater.reject(message=message)
                    terminal = "rejected"
                else:
                    await updater.failed(message=message)
            except CoordinatorFailure as exc:
                span.set_attribute("g3.status", exc.code)
                audit("coordinator.task_failed", request_id=request_id, task_id=task_id, mode=mode, code=exc.code)
                message = _agent_message(_safe_error(exc.code, str(exc)))
                if exc.code in {"invalid_business_request", "invalid_session", "idempotency_conflict"}:
                    await updater.reject(message=message)
                    terminal = "rejected"
                else:
                    await updater.failed(message=message)
            except Exception:
                LOGGER.exception("BYO Coordinator execution failed", extra={"task_id": task_id})
                span.set_attribute("g3.status", "internal_error")
                audit("coordinator.task_failed", request_id=request_id, task_id=task_id, mode=mode,
                      code="internal_error")
                await updater.failed(message=_agent_message(_safe_error("internal_error", "Coordinator failed closed")))
            finally:
                record_invocation(APP_NAME, mode, terminal)
                with self._lock:
                    self._cancellations.pop(task_id, None)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((asyncio.get_running_loop().time() - started) * 1000)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("task_id and context_id are required for cancellation")
        with self._lock:
            cancellation = self._cancellations.get(context.task_id)
            intake_task = self._intake_tasks.get(context.task_id)
        if cancellation is not None:
            cancellation.set()
        if intake_task is not None:
            intake_task.cancel()
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(message=_agent_message(_safe_error("cancelled", "Coordinator task cancelled")))


def _on_screen(result, task_id: str | None) -> None:
    record_prompt_screen(APP_NAME, result)
    if result.detected:
        audit("coordinator.prompt_pii_masked", task_id=task_id, mode="natural-language",
              pii_types=",".join(sorted(name for name, count in result.counts.items() if count)))


def _agent_message(text: str):
    from a2a.utils import new_agent_text_message

    return new_agent_text_message(text)


def agent_card() -> AgentCard:
    """Return the Agent Card matching the existing fraud-coordinator identity."""

    return AgentCard(
        name="fraud_coordinator",
        description="Fraud Investigation Coordinator",
        version=APP_VERSION,
        url=APP_URL,
        preferredTransport="JSONRPC",
        protocolVersion="0.3",
        # kagent Chat always uses message/stream; the executor publishes the same
        # TaskUpdater events for streaming and non-streaming JSON-RPC calls.
        capabilities=AgentCapabilities(streaming=True, stateTransitionHistory=True),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[AgentSkill(
            id="investigate-transaction",
            name="Investigate a transaction",
            description=(
                "Grounded fraud investigation with model score, features and policy citations. "
                "Accepts the typed CALL_AGENT=/REQUEST_JSON= protocol or one natural-language question "
                "naming the transaction, user, account, device (merchant for payments) UUIDs, amount, "
                "channel and ISO-8601 event_timestamp."
            ),
            inputModes=["text"],
            outputModes=["text"],
            tags=["fraud", "investigation"],
        )],
    )


def build_app(
    *,
    task_store: TaskStore | None = None,
    executor: AgentExecutor | None = None,
) -> FastAPI:
    """Build the production/local FastAPI app from public A2A SDK contracts."""

    configure_telemetry(APP_NAME, APP_VERSION)
    initialize_agent_metrics(APP_NAME)
    flush_agent_metrics()
    resources = AppResources(None, None)
    if task_store is None:
        task_store, resources = controller_task_store(APP_NAME)
    effective_executor = executor or CoordinatorExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=InflightTrackingExecutor(APP_NAME, effective_executor),
        task_store=task_store,
        request_context_builder=KAgentRequestContextBuilder(task_store=task_store),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if resources.controller_client is not None:
            await resources.controller_client.aclose()

    app = FastAPI(title="G3 BYO Fraud Coordinator", version=APP_VERSION, lifespan=lifespan)
    app.add_api_route("/health", lambda: PlainTextResponse("OK"), methods=["GET"])

    def ready() -> PlainTextResponse:
        if resources.token_hook is not None and not resources.token_hook.available():
            return PlainTextResponse("projected kagent token unavailable", status_code=503)
        if isinstance(effective_executor, CoordinatorExecutor):
            try:
                effective_executor.ready()
            except Exception:
                LOGGER.warning("Shared idempotency authority is not ready", exc_info=True)
                return PlainTextResponse(
                    "shared idempotency authority unavailable",
                    status_code=503,
                )
        return PlainTextResponse("READY")

    app.add_api_route("/ready", ready, methods=["GET"])
    A2AFastAPIApplication(
        agent_card=agent_card(),
        http_handler=request_handler,
        max_content_length=MAX_REQUEST_BYTES,
    ).add_routes_to_app(app)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = build_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(app, host="0.0.0.0", port=8080, access_log=True)


if __name__ == "__main__":
    main()
