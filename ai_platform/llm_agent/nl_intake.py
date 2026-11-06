"""Bounded natural-language intake for the BYO fraud Coordinator.

An analyst question typed into kagent Chat is converted into the existing typed
``G3BusinessRequest``.  The LLM (vLLM behind agentgateway, native OpenAI tool
calling with vLLM's ``hermes`` parser) only *selects* the single approved tool
and *extracts* its arguments.  Every argument is then validated against the
analyst's own text, so an identifier, amount or timestamp that does not appear
verbatim is rejected instead of being invented.  Execution afterwards is the
unchanged deterministic Coordinator path; no business value comes from the LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
import re
from typing import Literal
from urllib.parse import urlparse

import httpx
from opentelemetry import propagate, trace
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.llm_agent.agent_ab import AgentExperiment, ExperimentPlanner
from ai_platform.llm_agent.business_request import G3BusinessRequest
from ai_platform.llm_agent.prompt_safety import ScreenResult, screen_prompt


APPROVED_LLM_HOST = "agentgateway.ewallet-agents.svc.cluster.local"
APPROVED_LLM_PORT = 3000
APPROVED_LLM_BASE_URL = f"http://{APPROVED_LLM_HOST}:{APPROVED_LLM_PORT}/v1"
DEFAULT_LLM_MODEL = "ewallet-fraud-llm"
TOOL_NAME = "investigate_transaction"
INTAKE_REVISION = "nl-intake/v1"
MAX_PROMPT_CHARS = 2_000
MAX_LLM_RESPONSE_BYTES = 65_536
MAX_LLM_TIMEOUT_SECONDS = 200.0
# Projection and policy query of the accepted N10-E1 business request; the LLM
# never chooses which features or which knowledge query are executed.
DEFAULT_FEATURE_NAMES = (
    "account_behavior__account_tx_count_1h",
    "account_behavior__account_tx_count_24h",
    "user_behavior__user_avg_amount_30d",
    "merchant_behavior__merchant_tx_count_24h",
)
DEFAULT_POLICY_QUERY = "payment anomaly policy and step-up authentication requirements"

# Short instruction measured on the deployed Qwen3-0.6B (N11 Gate A, temperature
# 0): a longer prompt with refusal guidance made the model answer in free text
# (an invented risk level) instead of calling the tool. Grounding and refusal
# are enforced in code below, never delegated to the prompt; free-text model
# content is never shown to the analyst.
SYSTEM_PROMPT = "You are a fraud operations assistant. Use the provided tool when it matches the request."

_ID = {"type": "string", "description": "UUID copied exactly from the user message"}
TOOL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Investigate one wallet transaction: Feast features, KServe fraud score and cited fraud policy.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "transaction_id": _ID,
                "transaction_type": {"type": "string", "enum": ["payment", "transfer", "deposit", "withdraw"]},
                "user_id": _ID,
                "account_id": _ID,
                "device_id": _ID,
                "merchant_id": _ID,
                "counterparty_account_id": _ID,
                "amount_vnd": {"type": "number"},
                "channel": {"type": "string", "enum": ["app", "web", "atm"]},
                "event_timestamp": {"type": "string", "description": "ISO-8601 timestamp with timezone"},
                "include_policy_citations": {"type": "boolean"},
            },
            "required": [
                "transaction_id", "transaction_type", "user_id", "account_id",
                "device_id", "amount_vnd", "channel", "event_timestamp",
            ],
        },
    },
}

_UUID = re.compile(r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F-])")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})"
)
_NUMBER = re.compile(r"(?<![\w.,-])\d[\d.,]*(?![\w-])")
# Longest/closest label wins; "counterparty account" must not become account_id.
_ROLE_LABELS: tuple[tuple[str, str], ...] = (
    ("counterparty_account_id", r"counterparty(?:[ _]account)?|tài khoản nhận|người nhận"),
    ("merchant_id", r"merchant|người bán|cửa hàng"),
    ("device_id", r"device|thiết bị"),
    ("account_id", r"account|tài khoản"),
    ("user_id", r"user|người dùng|khách hàng"),
    ("transaction_id", r"transaction|giao dịch|txn|payment|transfer|deposit|withdraw"),
)
_TYPE_WORDS = {
    "payment": r"\bpayment\b|thanh toán",
    "transfer": r"\btransfer\b|chuyển tiền|chuyển khoản",
    "deposit": r"\bdeposit\b|nạp tiền",
    "withdraw": r"\bwithdraw(?:al)?\b|rút tiền",
}
_CHANNEL_WORDS = {"app": r"\bapp\b|ứng dụng", "web": r"\bweb\b", "atm": r"\batm\b"}
_POLICY_WORDS = re.compile(r"chính sách|policy|trích dẫn|citation|quy định", re.I)
_ID_FIELDS = ("transaction_id", "user_id", "account_id", "device_id", "merchant_id", "counterparty_account_id")


class IntakeFailure(ValueError):
    """Stable, user-visible rejection; never carries backend or LLM output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvestigateArguments(BaseModel):
    """Exact tool arguments; unknown keys from the model are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: str = Field(min_length=36, max_length=36)
    transaction_type: Literal["payment", "transfer", "deposit", "withdraw"]
    user_id: str = Field(min_length=36, max_length=36)
    account_id: str = Field(min_length=36, max_length=36)
    device_id: str = Field(min_length=36, max_length=36)
    merchant_id: str | None = Field(default=None, min_length=36, max_length=36)
    counterparty_account_id: str | None = Field(default=None, min_length=36, max_length=36)
    amount_vnd: float = Field(gt=0, allow_inf_nan=False)
    channel: Literal["app", "web", "atm"]
    event_timestamp: str = Field(min_length=16, max_length=40)
    include_policy_citations: bool = False


@dataclass(frozen=True)
class ToolSelection:
    """One structured tool call returned by the LLM, plus audit metadata."""

    name: str
    arguments: Mapping[str, object]
    response_id: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    variant: str = ""
    served_model: str = ""
    backend: str = ""


@dataclass(frozen=True)
class IntakeResult:
    request: G3BusinessRequest
    selection: ToolSelection


def is_typed_wire_text(text: str) -> bool:
    """The established two-line protocol keeps its exact deterministic path."""

    return text.startswith("CALL_AGENT=")


def _validate_base_url(value: str) -> None:
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


def _environment_key() -> str:
    return os.getenv("EWALLET_LLM_API_KEY", "").strip()


class OpenAIToolPlanner:
    """Native OpenAI-compatible tool selection through the shared agentgateway."""

    def __init__(
        self,
        *,
        base_url: str = APPROVED_LLM_BASE_URL,
        model: str = DEFAULT_LLM_MODEL,
        timeout_seconds: float = 190.0,
        key_reader: Callable[[], str] = _environment_key,
        transport: httpx.AsyncBaseTransport | None = None,
        temperature: float = 0,
        max_tokens: int = 384,
        variant: str = "",
    ) -> None:
        _validate_base_url(base_url)
        if not 1.0 <= timeout_seconds <= MAX_LLM_TIMEOUT_SECONDS:
            raise ValueError("LLM timeout must be between 1 and 200 seconds")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", model):
            raise ValueError("LLM model name is invalid")
        self._url = f"{base_url}/chat/completions"
        self._model = model
        self._timeout = timeout_seconds
        self._key_reader = key_reader
        self._transport = transport
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.variant = variant

    def available(self) -> bool:
        return bool(self._key_reader().strip())

    def for_context(self, context_id: str) -> "OpenAIToolPlanner":
        """Without an experiment every conversation uses this planner."""
        return self

    def _body(self, text: str) -> dict[str, object]:
        return {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": False,
            "tool_choice": "auto",
            "tools": [TOOL_SCHEMA],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }

    async def select(self, text: str) -> ToolSelection:
        key = self._key_reader().strip()
        if not key:
            raise IntakeFailure("llm_credential_unavailable", "Coordinator LLM credential is unavailable")
        headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
        propagate.inject(headers)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False, transport=self._transport,
            ) as client:
                response = await client.post(self._url, headers=headers, json=self._body(text))
        except httpx.TimeoutException as exc:
            raise IntakeFailure("llm_timeout", "LLM tool selection exceeded its deadline") from exc
        except httpx.HTTPError as exc:
            raise IntakeFailure("llm_unavailable", "LLM gateway request failed") from exc
        if response.status_code != 200:
            raise IntakeFailure("llm_unavailable", f"LLM gateway returned HTTP {response.status_code}")
        if len(response.content) > MAX_LLM_RESPONSE_BYTES:
            raise IntakeFailure("llm_response_too_large", "LLM response exceeds its byte budget")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            usage = payload.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise IntakeFailure("llm_invalid_response", "LLM response is not a chat completion") from exc
        calls = message.get("tool_calls") or []
        if not calls:
            raise IntakeFailure(
                "unsupported_request",
                "Yêu cầu không phải là một điều tra gian lận cho một giao dịch cụ thể; không có công cụ nào được chọn",
            )
        if len(calls) != 1:
            raise IntakeFailure("multiple_tool_calls", "LLM selected more than one tool")
        try:
            function = calls[0]["function"]
            name = function["name"]
            raw_arguments = function["arguments"]
        except (KeyError, TypeError) as exc:
            raise IntakeFailure("llm_invalid_response", "LLM tool call is malformed") from exc
        if name != TOOL_NAME:
            raise IntakeFailure("unauthorized_tool", "LLM selected a tool that the Coordinator does not grant")
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise IntakeFailure("invalid_tool_arguments", "LLM tool arguments are not valid JSON") from exc
        if not isinstance(arguments, dict):
            raise IntakeFailure("invalid_tool_arguments", "LLM tool arguments must be an object")
        return ToolSelection(
            name=name,
            arguments=arguments,
            response_id=str(payload.get("id", ""))[:128],
            finish_reason=str(choice.get("finish_reason", ""))[:32],
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            variant=self.variant,
            served_model=str(payload.get("model", ""))[:128],
            backend=response.headers.get("x-ewallet-backend", "")[:64],
        )


def planner_from_env() -> OpenAIToolPlanner | ExperimentPlanner | None:
    """Natural-language intake is opt-in and bound to the approved LLM route.

    ``EWALLET_AGENT_AB_CONFIG`` replaces the single model with two configured
    variants on the same route; a malformed document stops the Coordinator.
    """

    if os.getenv("EWALLET_NL_INTAKE_ENABLED", "false").lower() != "true":
        return None
    base_url = os.getenv("EWALLET_LLM_BASE_URL", APPROVED_LLM_BASE_URL).strip()
    timeout_seconds = float(os.getenv("EWALLET_LLM_TIMEOUT_SECONDS", "190"))
    raw_experiment = os.getenv("EWALLET_AGENT_AB_CONFIG", "").strip()
    if raw_experiment:
        experiment = AgentExperiment.from_json(raw_experiment)
        return ExperimentPlanner(
            experiment,
            {
                variant.name: OpenAIToolPlanner(
                    base_url=base_url,
                    model=variant.model,
                    timeout_seconds=timeout_seconds,
                    temperature=variant.temperature,
                    max_tokens=variant.max_tokens,
                    variant=variant.name,
                )
                for variant in experiment.variants
            },
        )
    return OpenAIToolPlanner(
        base_url=base_url,
        model=os.getenv("EWALLET_LLM_MODEL", DEFAULT_LLM_MODEL).strip(),
        timeout_seconds=timeout_seconds,
    )


def labelled_identifiers(text: str) -> dict[str, str]:
    """Deterministically assign each UUID in the text to its closest preceding label."""

    roles: dict[str, str] = {}
    for match in _UUID.finditer(text):
        window = text[max(0, match.start() - 48):match.start()].lower()
        best: tuple[int, int, str] | None = None
        for role, pattern in _ROLE_LABELS:
            for label in re.finditer(pattern, window):
                candidate = (label.end(), label.end() - label.start(), role)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        if best is None:
            continue
        role, value = best[2], match.group(0).lower()
        if role in roles and roles[role] != value:
            raise IntakeFailure("ambiguous_identifier", f"Có nhiều giá trị cho {role}; hãy chỉ ra đúng một giao dịch")
        roles[role] = value
    return roles


def _numbers(text: str) -> list[float]:
    values: list[float] = []
    for token in _NUMBER.findall(text):
        token = token.rstrip(".,")
        for candidate in (token, token.replace(",", ""), token.replace(".", "").replace(",", ".")):
            try:
                value = float(candidate)
            except ValueError:
                continue
            if math.isfinite(value):
                values.append(value)
    return values


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))


def _single_mention(text: str, words: Mapping[str, str], field: str) -> str:
    found = [name for name, pattern in words.items() if re.search(pattern, text, re.I)]
    if len(found) != 1:
        raise IntakeFailure(
            "missing_or_ambiguous_field",
            f"Cần chỉ rõ đúng một {field} của giao dịch (tìm thấy {len(found)})",
        )
    return found[0]


def ground_arguments(text: str, raw: Mapping[str, object]) -> InvestigateArguments:
    """Accept LLM arguments only when each value is literally present in the text."""

    normalized = {
        key: value for key, value in raw.items()
        if not (key in {"merchant_id", "counterparty_account_id"} and value in ("", None))
    }
    try:
        arguments = InvestigateArguments.model_validate(normalized)
    except ValidationError as exc:
        raise IntakeFailure("invalid_tool_arguments", "LLM tool arguments failed schema validation") from exc

    present = {item.group(0).lower() for item in _UUID.finditer(text)}
    labelled = labelled_identifiers(text)
    chosen: dict[str, str] = {}
    for field in _ID_FIELDS:
        value = getattr(arguments, field)
        if value is None:
            continue
        value = value.lower()
        if value not in present:
            raise IntakeFailure("ungrounded_argument", f"{field} không xuất hiện trong câu hỏi")
        if field in labelled and labelled[field] != value:
            raise IntakeFailure("ungrounded_argument", f"{field} không khớp với nhãn trong câu hỏi")
        chosen[field] = value
    for field, value in labelled.items():
        if chosen.get(field) != value:
            raise IntakeFailure("ungrounded_argument", f"{field} trong câu hỏi không được giữ nguyên")
    if len(set(chosen.values())) != len(chosen):
        raise IntakeFailure("ambiguous_identifier", "Một định danh được dùng cho nhiều vai trò")

    if not any(math.isclose(arguments.amount_vnd, value, rel_tol=0, abs_tol=0.005) for value in _numbers(text)):
        raise IntakeFailure("ungrounded_argument", "Số tiền không xuất hiện trong câu hỏi")
    stamps = [_parse_timestamp(item.group(0)) for item in _TIMESTAMP.finditer(text)]
    if not stamps:
        raise IntakeFailure("missing_or_ambiguous_field", "Cần event_timestamp ISO-8601 có múi giờ của giao dịch")
    try:
        chosen_stamp = _parse_timestamp(arguments.event_timestamp)
    except ValueError as exc:
        raise IntakeFailure("invalid_tool_arguments", "event_timestamp is not ISO-8601") from exc
    if chosen_stamp.tzinfo is None or chosen_stamp not in stamps:
        raise IntakeFailure("ungrounded_argument", "event_timestamp không xuất hiện trong câu hỏi")
    if arguments.transaction_type != _single_mention(text, _TYPE_WORDS, "loại giao dịch"):
        raise IntakeFailure("ungrounded_argument", "transaction_type không khớp với câu hỏi")
    if arguments.channel != _single_mention(text, _CHANNEL_WORDS, "kênh giao dịch"):
        raise IntakeFailure("ungrounded_argument", "channel không khớp với câu hỏi")
    return arguments


def request_id_for(context_id: str, message_id: str) -> str:
    digest = hashlib.sha256(f"{context_id}\0{message_id}".encode("utf-8")).hexdigest()[:32]
    return f"nl-{digest}"


def business_request(text: str, arguments: InvestigateArguments, request_id: str) -> G3BusinessRequest:
    """Build the existing typed contract; every field is validated by its own model."""

    try:
        transaction = FraudPredictionRequest(
            transaction_id=arguments.transaction_id.lower(),
            event_timestamp=_parse_timestamp(arguments.event_timestamp),
            user_id=arguments.user_id.lower(),
            account_id=arguments.account_id.lower(),
            device_id=arguments.device_id.lower(),
            merchant_id=arguments.merchant_id.lower() if arguments.merchant_id else None,
            counterparty_account_id=(
                arguments.counterparty_account_id.lower() if arguments.counterparty_account_id else None
            ),
            type=arguments.transaction_type,
            amount=arguments.amount_vnd,
            currency="VND",
            channel=arguments.channel,
        )
    except (ValidationError, ValueError) as exc:
        raise IntakeFailure("missing_or_ambiguous_field", f"Giao dịch thiếu trường bắt buộc: {exc.__class__.__name__}") from exc
    with_policy = arguments.include_policy_citations or bool(_POLICY_WORDS.search(text))
    tools = ("feature_lookup", "fraud_scoring") + (("knowledge_search",) if with_policy else ())
    return G3BusinessRequest(
        request_id=request_id,
        target_agent="feature-rag-specialist",
        capabilities=("fraud-evidence",),
        requested_tools=tools,
        transaction=transaction,
        feature_names=DEFAULT_FEATURE_NAMES,
        knowledge_query=DEFAULT_POLICY_QUERY if with_policy else None,
    )


async def plan(
    text: str,
    *,
    planner: OpenAIToolPlanner,
    context_id: str,
    message_id: str,
    on_screen: Callable[[ScreenResult], None] | None = None,
) -> IntakeResult:
    """Screen PII, pre-check, call the LLM once, ground its arguments and build the typed request.

    The PII screen runs first: masked text is the only text the LLM and the
    grounding step ever see, so a detected value can never become an argument.
    """

    text = text.strip()
    if not text or len(text) > MAX_PROMPT_CHARS:
        raise IntakeFailure("invalid_prompt", f"Câu hỏi phải có từ 1 đến {MAX_PROMPT_CHARS} ký tự")
    screen = screen_prompt(text)
    if on_screen is not None:
        on_screen(screen)
    text = screen.text
    if not _UUID.search(text):
        raise IntakeFailure(
            "missing_transaction_identifiers",
            "Agent này chỉ hỗ trợ điều tra gian lận cho một giao dịch cụ thể; hãy cung cấp transaction, "
            "user, account, device (và merchant với payment) dạng UUID, số tiền, kênh và event_timestamp",
        )
    labelled_identifiers(text)  # fail fast on ambiguous identifiers before spending an LLM call
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("g3.coordinator.nl_intake.llm_tool_selection") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", planner._model)
        selection = await planner.select(text)
        span.set_attribute("gen_ai.tool.name", selection.name)
        span.set_attribute("gen_ai.response.id", selection.response_id)
        span.set_attribute("gen_ai.response.finish_reasons", [selection.finish_reason])
        span.set_attribute("gen_ai.usage.input_tokens", selection.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", selection.completion_tokens)
        if selection.variant:
            span.set_attribute("ewallet.ab.variant", selection.variant)
            span.set_attribute("gen_ai.response.model", selection.served_model)
            span.set_attribute("ewallet.llm.backend", selection.backend)
    arguments = ground_arguments(text, selection.arguments)
    request = business_request(text, arguments, request_id_for(context_id, message_id))
    return IntakeResult(request=request, selection=selection)


def render_summary(result: Mapping[str, object], request: G3BusinessRequest, selection: ToolSelection) -> str:
    """Deterministic analyst answer; every value is copied from validated evidence."""

    tx = request.transaction
    lines = [
        "**Kết quả điều tra gian lận**",
        "",
        f"- Giao dịch `{tx.transaction_id}`: {tx.type.value}, {tx.amount} {tx.currency}, kênh {tx.channel.value}, "
        f"thời điểm {tx.event_timestamp.isoformat()}",
    ]
    score = result.get("fraud_score")
    if isinstance(score, Mapping):
        verdict = "GIAN LẬN" if score.get("fraud_prediction") == 1 else "không gian lận"
        lines.append(
            f"- Mô hình `{score.get('model_name')}` v{score.get('model_version')}: "
            f"fraud_probability = {score.get('fraud_probability')}, dự đoán = {score.get('fraud_prediction')} ({verdict}); "
            f"feature_timestamp {score.get('feature_timestamp')}"
        )
    features = result.get("features")
    if isinstance(features, Mapping) and features:
        lines.append("- Feature từ Feast online store:")
        for name in sorted(features):
            item = features[name]
            if isinstance(item, Mapping):
                lines.append(f"  - `{name}` = {item.get('value')} (event_timestamp {item.get('event_timestamp')})")
    citations = result.get("citations")
    if isinstance(citations, list) and citations:
        lines.append("- Chính sách liên quan (trích dẫn từ RAG):")
        for index, item in enumerate(citations, start=1):
            if isinstance(item, Mapping):
                lines.append(
                    f"  {index}. `{item.get('document_id')}` / `{item.get('chunk_id')}` "
                    f"(score {item.get('score')}, {item.get('source_uri')})"
                )
    unavailable = result.get("unavailable")
    if isinstance(unavailable, list) and unavailable:
        lines.append(f"- Không khả dụng: {json.dumps(unavailable, ensure_ascii=False)}")
    lines += [
        "",
        f"Trạng thái: `{result.get('status')}` · request_id `{request.request_id}` · "
        f"LLM chọn công cụ `{selection.name}` ({selection.prompt_tokens}+{selection.completion_tokens} token) · "
        f"{INTAKE_REVISION}",
        "",
        "_Các giá trị được sao chép nguyên văn từ bằng chứng đã xác thực của Specialist (Feast, KServe, RAG); "
        "LLM chỉ dùng để chọn công cụ và trích xuất tham số từ câu hỏi._",
    ]
    return "\n".join(lines)
