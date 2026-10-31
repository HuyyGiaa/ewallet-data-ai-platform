"""Bounded, fail-closed execution of typed tools behind the shared contract.

The executor owns concurrency, deadlines and the mapping of failures onto the
shared ``ToolCallResponse`` statuses. It never logs tool arguments or results.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import json
import logging
import time
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ValidationError

from ai_platform.inference.errors import (
    FeatureBackendError,
    FeatureTimeoutError,
    InferenceError,
    MissingFeatureError,
    ModelTimeoutError,
    StaleFeatureError,
)
from ai_platform.llm_agent.contracts import (
    TOOL_NAMES,
    Citation,
    RequestContext,
    ToolCallResponse,
    VersionContext,
)


class ToolRejectedError(Exception):
    """The request is well-formed but not acceptable (unknown baseline, overlap...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ToolUnavailableError(Exception):
    """A required backend is not configured or not ready; never synthesize a result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ToolContractError(Exception):
    """A backend answered, but its answer violates the tool contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True)
class ToolOutcome:
    result: Mapping[str, Any]
    citations: tuple[Citation, ...] = ()
    version_overrides: Mapping[str, str] = field(default_factory=dict)


class ToolHandler(Protocol):
    name: str
    description: str
    input_model: type[BaseModel]

    async def run(self, arguments: Any, context: RequestContext) -> ToolOutcome: ...

    async def ready(self) -> None: ...


_UNAVAILABLE = (FeatureBackendError, MissingFeatureError, StaleFeatureError)
_TIMEOUT = (FeatureTimeoutError, ModelTimeoutError)
HTTP_STATUS = MappingProxyType(
    {"ok": 200, "rejected": 422, "timeout": 504, "unavailable": 503, "error": 502}
)


def _error_result(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def _validation_details(error: ValidationError) -> list[dict[str, Any]]:
    # Input values are deliberately omitted: arguments may contain identifiers.
    return [
        {"location": [str(part) for part in item["loc"]], "type": item["type"]}
        for item in error.errors(include_url=False, include_input=False)
    ]


class ToolExecutor:
    def __init__(
        self,
        handlers: tuple[ToolHandler, ...],
        *,
        versions: VersionContext,
        max_concurrency: int = 8,
        max_timeout_ms: int = 30_000,
        logger: logging.Logger | None = None,
    ) -> None:
        names = [handler.name for handler in handlers]
        if not handlers or len(set(names)) != len(names) or set(names) - TOOL_NAMES:
            raise ValueError("handlers must be unique shared-contract tools")
        if not 1 <= max_concurrency <= 64:
            raise ValueError("max_concurrency must be in [1, 64]")
        if not 100 <= max_timeout_ms <= 120_000:
            raise ValueError("max_timeout_ms must be in [100, 120000]")
        self._handlers = MappingProxyType({handler.name: handler for handler in handlers})
        self._versions = versions
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_timeout_ms = max_timeout_ms
        self._logger = logger or logging.getLogger("ewallet.llm_agent.tools")

    @property
    def handlers(self) -> Mapping[str, ToolHandler]:
        return self._handlers

    def _response(
        self, context: RequestContext, tool_name: str, status: str,
        result: Mapping[str, Any], outcome: ToolOutcome | None = None,
    ) -> ToolCallResponse:
        versions = self._versions
        citations: tuple[Citation, ...] = ()
        if outcome is not None:
            versions = replace(versions, **dict(outcome.version_overrides))
            citations = outcome.citations
        return ToolCallResponse(
            request_id=context.request_id, tool_name=tool_name, status=status,
            result=dict(result), versions=versions, citations=citations,
        )

    async def call(
        self, tool_name: str, arguments: Mapping[str, Any] | BaseModel, context: RequestContext,
    ) -> ToolCallResponse:
        """Run one tool; the response status is the only success signal."""

        if tool_name not in TOOL_NAMES:
            raise ValueError(f"tool_name must be one of {sorted(TOOL_NAMES)}")
        started = time.monotonic()
        response = await self._call(tool_name, arguments, context)
        error = response.result.get("error") if response.status != "ok" else None
        self._logger.info(json.dumps({
            "event": "tool_call_completed",
            "request_id": context.request_id,
            "actor_id": context.actor_id,
            "tool_name": tool_name,
            "status": response.status,
            "error_code": error.get("code") if isinstance(error, Mapping) else None,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }, sort_keys=True))
        return response

    async def _call(
        self, tool_name: str, arguments: Mapping[str, Any] | BaseModel, context: RequestContext,
    ) -> ToolCallResponse:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return self._response(context, tool_name, "rejected", _error_result(
                "tool_not_served", "This service does not serve the requested tool", retryable=False))
        if isinstance(arguments, handler.input_model):
            parsed = arguments  # already validated by the typed FastAPI route
        elif not isinstance(arguments, Mapping):
            return self._response(context, tool_name, "rejected", _error_result(
                "invalid_arguments", "Tool arguments must be an object", retryable=False))
        else:
            try:
                parsed = handler.input_model.model_validate(dict(arguments))
            except ValidationError as exc:
                result = _error_result("invalid_arguments", "Tool arguments failed validation", retryable=False)
                result["error"]["details"] = _validation_details(exc)
                return self._response(context, tool_name, "rejected", result)

        timeout = min(context.timeout_ms, self._max_timeout_ms) / 1000
        try:
            outcome = await asyncio.wait_for(self._run(handler, parsed, context), timeout=timeout)
        except asyncio.TimeoutError:
            return self._response(context, tool_name, "timeout", _error_result(
                "tool_timeout", "Tool execution exceeded its deadline", retryable=True))
        except ToolRejectedError as exc:
            return self._response(context, tool_name, "rejected",
                                  _error_result(exc.code, exc.public_message, retryable=False))
        except ToolUnavailableError as exc:
            return self._response(context, tool_name, "unavailable",
                                  _error_result(exc.code, exc.public_message, retryable=True))
        except ToolContractError as exc:
            return self._response(context, tool_name, "error",
                                  _error_result(exc.code, exc.public_message, retryable=False))
        except _TIMEOUT as exc:
            return self._response(context, tool_name, "timeout",
                                  _error_result(exc.code, exc.public_message, retryable=True))
        except _UNAVAILABLE as exc:
            return self._response(context, tool_name, "unavailable",
                                  _error_result(exc.code, exc.public_message, retryable=exc.retryable))
        except InferenceError as exc:
            status = "unavailable" if exc.status_code == 503 else "error"
            return self._response(context, tool_name, status,
                                  _error_result(exc.code, exc.public_message, retryable=exc.retryable))
        except Exception:  # noqa: BLE001 - sanitized: no exception text leaves the service
            return self._response(context, tool_name, "error", _error_result(
                "internal_error", "Internal tool error", retryable=False))
        try:
            return self._response(context, tool_name, "ok", outcome.result, outcome)
        except (TypeError, ValueError):
            return self._response(context, tool_name, "error", _error_result(
                "provenance_contract_error", "Tool provenance violates the shared contract", retryable=False))

    async def _run(self, handler: ToolHandler, arguments: Any, context: RequestContext) -> ToolOutcome:
        async with self._semaphore:
            return await handler.run(arguments, context)

    async def readiness(self) -> dict[str, dict[str, str]]:
        """Return per-tool readiness; a tool is ready only if its backend answered."""

        report: dict[str, dict[str, str]] = {}
        for name, handler in self._handlers.items():
            try:
                await asyncio.wait_for(handler.ready(), timeout=self._max_timeout_ms / 1000)
                report[name] = {"status": "ready"}
            except (ToolUnavailableError, ToolContractError, ToolRejectedError) as exc:
                report[name] = {"status": "not_ready", "code": exc.code, "reason": exc.public_message}
            except InferenceError as exc:
                report[name] = {"status": "not_ready", "code": exc.code, "reason": exc.public_message}
            except asyncio.TimeoutError:
                report[name] = {"status": "not_ready", "code": "readiness_timeout", "reason": "Backend readiness timed out"}
            except Exception:  # noqa: BLE001 - sanitized readiness
                report[name] = {"status": "not_ready", "code": "readiness_error", "reason": "Backend readiness failed"}
        return report
