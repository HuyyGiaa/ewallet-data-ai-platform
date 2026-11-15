"""Bounded capability routing for the Coordinator: fraud, drift or both.

One analyst question may ask for a fraud investigation of one transaction, a
drift analysis of one monitored feature, or both. The Coordinator offers the LLM
(agentgateway -> vLLM, one call) two tools:

- ``investigate_transaction``: the unchanged N11 fraud tool and grounding;
- ``analyze_feature_drift``: feature, baseline revision and current window.

The LLM only selects tools and extracts arguments. A deterministic router then
decides what runs:

1. Every drift argument must be literally present and labelled in the text:
   exactly one monitored feature name, ``baseline <id>`` and
   ``window <start> to <end>`` with ISO-8601 timestamps. The router extracts them
   itself; the LLM's values must equal them.
2. A question that explicitly asks for drift (``drift``/``PSI``/``trôi``) with
   complete drift arguments gets the drift capability even when the small model
   emitted only the fraud call (``selection_source: deterministic``). Fraud is
   never added deterministically: its arguments need the LLM extraction plus the
   existing grounding.
3. A question that names transaction identifiers but for which the LLM did not
   select the fraud tool is rejected, never silently narrowed.

Nothing invents an identifier, feature name, baseline revision, window, score,
PSI value or citation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import re

from opentelemetry import trace
from pydantic import ValidationError

from ai_platform.llm_agent.business_request import (
    MONITORED_DRIFT_FEATURES,
    G3BusinessRequest,
    G3DriftRequest,
    G3WorkflowRequest,
    TypedRequest,
    member_request_id,
)
from ai_platform.llm_agent.nl_intake import (
    INTAKE_REVISION,
    MAX_PROMPT_CHARS,
    TOOL_NAME,
    TOOL_SCHEMA,
    IntakeFailure,
    MultiSelection,
    OpenAIToolPlanner,
    ToolSelection,
    _TIMESTAMP,
    _UUID,
    _parse_timestamp,
    business_request,
    ground_arguments,
    labelled_identifiers,
    render_summary,
    request_id_for,
)
from ai_platform.llm_agent.prompt_safety import ScreenResult, screen_prompt

DRIFT_TOOL_NAME = "analyze_feature_drift"
WORKFLOW_INTAKE_REVISION = "workflow-intake/v1"
DRIFT_TOOL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": DRIFT_TOOL_NAME,
        "description": (
            "Compute PSI drift of one monitored model feature over a current time window "
            "against an approved baseline revision."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "feature_name": {"type": "string", "description": "Feature name copied exactly from the user message"},
                "baseline_id": {"type": "string", "description": "Baseline revision copied exactly from the user message"},
                "window_start": {"type": "string", "description": "ISO-8601 window start with timezone"},
                "window_end": {"type": "string", "description": "ISO-8601 window end with timezone"},
            },
            "required": ["feature_name", "baseline_id", "window_start", "window_end"],
        },
    },
}
WORKFLOW_TOOLS = (TOOL_SCHEMA, DRIFT_TOOL_SCHEMA)
SYSTEM_TOOLS = {TOOL_NAME: "fraud-evidence", DRIFT_TOOL_NAME: "drift"}

_DRIFT_INTENT = re.compile(r"\bdrift\b|\bpsi\b|trôi|độ lệch phân phối", re.I)
_BASELINE = re.compile(r"\bbaseline(?:[ _]id)?\b\s*[:=]?\s*`?([A-Za-z0-9][A-Za-z0-9._:@/-]{0,127})", re.I)
_TS = _TIMESTAMP.pattern
_WINDOW = re.compile(
    r"(?:\bwindow\b|cửa sổ|current_window|khoảng)[^0-9]{0,40}?(" + _TS + r")\s*"
    r"(?:to|đến|tới|until|->|→|–|—|\.\.)\s*(" + _TS + r")",
    re.I,
)
_FEATURE_TOKEN = re.compile(r"[a-z_]+__[a-z0-9_]+")
# The bare word "amount" is also the transaction amount; as a drift feature it must be labelled.
_AMOUNT_FEATURE = re.compile(r"\bfeature(?:[ _]name)?\s*[:=]?\s*`?amount\b", re.I)


@dataclass(frozen=True)
class DriftArguments:
    feature_name: str
    baseline_id: str
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class WorkflowIntakeResult:
    """The typed request to execute and how each capability was selected."""

    request: TypedRequest
    response: MultiSelection
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def selection(self) -> ToolSelection:
        """One audit record for the whole LLM response (A/B metrics, audit, rendering)."""

        return ToolSelection(
            name=",".join(sorted(self.sources)) or "none",
            arguments={},
            response_id=self.response.response_id,
            finish_reason=self.response.finish_reason,
            prompt_tokens=self.response.prompt_tokens,
            completion_tokens=self.response.completion_tokens,
            variant=self.response.variant,
            served_model=self.response.served_model,
            backend=self.response.backend,
        )


def drift_requested(text: str) -> bool:
    return bool(_DRIFT_INTENT.search(text))


def extract_drift_arguments(text: str) -> DriftArguments:
    """Deterministically read the labelled drift arguments; reject anything ambiguous."""

    found = {token for token in _FEATURE_TOKEN.findall(text) if token in MONITORED_DRIFT_FEATURES}
    if _AMOUNT_FEATURE.search(text):
        found.add("amount")
    features = sorted(found)
    if len(features) != 1:
        raise IntakeFailure(
            "missing_or_ambiguous_field",
            f"Phân tích drift cần đúng một tên feature được giám sát trong câu hỏi (tìm thấy {len(features)})",
        )
    baselines = {match.group(1).rstrip(".,;)`") for match in _BASELINE.finditer(text)}
    if len(baselines) != 1:
        raise IntakeFailure("missing_or_ambiguous_field",
                            "Phân tích drift cần đúng một baseline (ví dụ: baseline drift-...)")
    windows = {(match.group(1), match.group(2)) for match in _WINDOW.finditer(text)}
    if len(windows) != 1:
        raise IntakeFailure("missing_or_ambiguous_field",
                            "Phân tích drift cần đúng một cửa sổ: window <ISO-8601> to <ISO-8601>")
    start_text, end_text = next(iter(windows))
    try:
        start, end = _parse_timestamp(start_text), _parse_timestamp(end_text)
    except ValueError as exc:
        raise IntakeFailure("invalid_tool_arguments", "Cửa sổ drift không phải ISO-8601") from exc
    return DriftArguments(features[0], next(iter(baselines)), start, end)


def ground_drift_arguments(text: str, raw: Mapping[str, object], expected: DriftArguments) -> DriftArguments:
    """Accept LLM drift arguments only when they equal the labelled values in the text."""

    if set(raw) != {"feature_name", "baseline_id", "window_start", "window_end"}:
        raise IntakeFailure("invalid_tool_arguments", "LLM drift arguments failed schema validation")
    if not all(isinstance(value, str) for value in raw.values()):
        raise IntakeFailure("invalid_tool_arguments", "LLM drift arguments must be strings")
    try:
        start = _parse_timestamp(str(raw["window_start"]))
        end = _parse_timestamp(str(raw["window_end"]))
    except ValueError as exc:
        raise IntakeFailure("invalid_tool_arguments", "LLM drift window is not ISO-8601") from exc
    chosen = DriftArguments(str(raw["feature_name"]), str(raw["baseline_id"]), start, end)
    if chosen != expected:
        raise IntakeFailure("ungrounded_argument", "Tham số drift của LLM không khớp với câu hỏi")
    return chosen


def drift_request(arguments: DriftArguments, request_id: str) -> G3DriftRequest:
    try:
        return G3DriftRequest(
            request_id=request_id,
            feature_name=arguments.feature_name,
            baseline_id=arguments.baseline_id,
            current_window={"start": arguments.window_start, "end": arguments.window_end},
        )
    except ValidationError as exc:
        raise IntakeFailure("invalid_tool_arguments", "Tham số drift không hợp lệ: "
                            + "; ".join(".".join(str(part) for part in item["loc"]) for item in exc.errors())) from exc


async def plan_workflow(
    text: str,
    *,
    planner: OpenAIToolPlanner,
    context_id: str,
    message_id: str,
    on_screen: Callable[[ScreenResult], None] | None = None,
) -> WorkflowIntakeResult:
    """Screen PII, pre-check, call the LLM once, ground every argument and build the typed request."""

    text = text.strip()
    if not text or len(text) > MAX_PROMPT_CHARS:
        raise IntakeFailure("invalid_prompt", f"Câu hỏi phải có từ 1 đến {MAX_PROMPT_CHARS} ký tự")
    screen = screen_prompt(text)
    if on_screen is not None:
        on_screen(screen)
    text = screen.text
    has_transaction = bool(_UUID.search(text))
    wants_drift = drift_requested(text)
    if not has_transaction and not wants_drift:
        raise IntakeFailure(
            "unsupported_request",
            "Coordinator hỗ trợ điều tra gian lận một giao dịch cụ thể (UUID, số tiền, kênh, event_timestamp) "
            "và/hoặc phân tích drift (feature, baseline, window)",
        )
    if has_transaction:
        labelled_identifiers(text)  # fail fast on ambiguous identifiers before spending an LLM call
    expected_drift = extract_drift_arguments(text) if wants_drift else None

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("g3.coordinator.nl_intake.llm_tool_selection") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", planner._model)
        # The deterministic screen above fixes the scope; the LLM only extracts the arguments.
        offered = ({TOOL_NAME} if has_transaction else set()) | ({DRIFT_TOOL_NAME} if wants_drift else set())
        span.set_attribute("ewallet.intake.offered_tools", sorted(offered))
        response = await planner.select_many(text, tools=offered)
        span.set_attribute("gen_ai.tool.name", [item.name for item in response.selections])
        span.set_attribute("gen_ai.response.id", response.response_id)
        span.set_attribute("gen_ai.response.finish_reasons", [response.finish_reason])
        span.set_attribute("gen_ai.usage.input_tokens", response.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.completion_tokens)
        if response.variant:
            span.set_attribute("ewallet.ab.variant", response.variant)

    # Window timestamps belong to the drift request; the fraud grounding must not
    # accept one of them as the transaction's event_timestamp or amount.
    fraud_text = _WINDOW.sub(" ", text) if expected_drift is not None else text
    sources: dict[str, str] = {}
    fraud_arguments = None
    drift_arguments: DriftArguments | None = None
    for selection in response.selections:
        if selection.name == TOOL_NAME:
            fraud_arguments = ground_arguments(fraud_text, selection.arguments)
            sources["fraud-evidence"] = "llm"
        else:
            if expected_drift is None:
                raise IntakeFailure("ungrounded_argument", "LLM chọn phân tích drift nhưng câu hỏi không yêu cầu drift")
            drift_arguments = ground_drift_arguments(text, selection.arguments, expected_drift)
            sources["drift"] = "llm"
    if has_transaction and fraud_arguments is None:
        raise IntakeFailure(
            "incomplete_tool_selection",
            "Câu hỏi có định danh giao dịch nhưng LLM không chọn điều tra giao dịch; yêu cầu không được thu hẹp âm thầm",
        )
    if expected_drift is not None and drift_arguments is None:
        drift_arguments = expected_drift
        sources["drift"] = "deterministic"

    request_id = request_id_for(context_id, message_id)
    request: TypedRequest
    if fraud_arguments is not None and drift_arguments is not None:
        request = G3WorkflowRequest(
            request_id=request_id,
            fraud=business_request(fraud_text, fraud_arguments, member_request_id(request_id, "fraud")),
            drift=drift_request(drift_arguments, member_request_id(request_id, "drift")),
        )
    elif fraud_arguments is not None:
        request = business_request(fraud_text, fraud_arguments, request_id)
    else:
        assert drift_arguments is not None
        request = drift_request(drift_arguments, request_id)
    trace.get_current_span().set_attribute("ewallet.workflow.capabilities", sorted(sources))
    return WorkflowIntakeResult(request=request, response=response, sources=sources)


def _drift_lines(section: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for item in section.get("drift") or []:
        if not isinstance(item, Mapping):
            continue
        window = item.get("current_window") or {}
        revisions = item.get("dataset_revisions") or {}
        lines.append(
            f"- Feature `{item.get('feature_name')}` so với baseline `{item.get('baseline_id')}`: "
            f"PSI = {item.get('psi')}, trạng thái `{item.get('drift_status')}`, "
            f"{item.get('current_count')} quan sát trong cửa sổ {window.get('start')} → {window.get('end')}"
        )
        lines.append(
            f"  - Dữ liệu: reference `{revisions.get('reference')}`, current `{revisions.get('current')}`; "
            f"ngưỡng {json.dumps(item.get('thresholds'), ensure_ascii=False)}"
        )
    for item in section.get("unavailable") or []:
        lines.append(f"- Không khả dụng: {json.dumps(item, ensure_ascii=False)}")
    return lines


def render_workflow_summary(result: Mapping[str, object], intake: WorkflowIntakeResult) -> str:
    """Deterministic analyst answer: each block is labelled with the Specialist that produced it."""

    request = intake.request
    if isinstance(request, G3BusinessRequest):
        return render_summary(result, request, intake.selection)
    specialists = result.get("specialists")
    specialists = specialists if isinstance(specialists, Mapping) else {}
    lines = ["**Kết quả điều phối (Fraud Coordinator)**", ""]
    fraud = specialists.get("feature-rag-specialist")
    if isinstance(request, G3WorkflowRequest):
        lines.append("### Từ feature-rag-specialist (điều tra gian lận)")
        if isinstance(fraud, Mapping) and fraud.get("status") != "failed":
            body = render_summary({**fraud, "status": fraud.get("status")}, request.fraud, intake.selection)
            lines += [line for line in body.splitlines()[2:] if not line.startswith(("Trạng thái:", "_Các giá trị"))]
        else:
            error = fraud.get("error") if isinstance(fraud, Mapping) else None
            lines.append(f"- Không có kết quả: {json.dumps(error, ensure_ascii=False)}")
        lines.append("")
    drift = specialists.get("drift-specialist")
    lines.append("### Từ drift-specialist (phân tích drift)")
    if isinstance(drift, Mapping) and drift.get("status") != "failed":
        lines += _drift_lines(drift)
    else:
        error = drift.get("error") if isinstance(drift, Mapping) else None
        lines.append(f"- Không có kết quả: {json.dumps(error, ensure_ascii=False)}")
    selected = ", ".join(f"{name} ({source})" for name, source in sorted(intake.sources.items()))
    lines += [
        "",
        f"Trạng thái: `{result.get('status')}` · request_id `{result.get('request_id')}` · "
        f"capability: {selected} · LLM {intake.response.prompt_tokens}+{intake.response.completion_tokens} token · "
        f"{WORKFLOW_INTAKE_REVISION}/{INTAKE_REVISION}",
        "",
        "_Mọi giá trị được sao chép nguyên văn từ bằng chứng đã xác thực của từng Specialist "
        "(Feast, KServe, RAG, drift baseline/window); LLM chỉ chọn công cụ và trích xuất tham số._",
    ]
    return "\n".join(lines)
