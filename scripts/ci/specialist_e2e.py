"""Direct A2A verification of one SandboxAgent Specialist with a typed fixture and pinned known answers.

The candidate SandboxAgents cannot be reached through the Coordinator: the A2A Gateway's sandbox
routes and the Coordinator's gateway hostname are fixed to the production Specialist names (frozen
application and production Helm values). The kagent controller serves every SandboxAgent at
``/api/a2a-sandboxes/<namespace>/<agent>/``, so a candidate is exercised there, one typed request
per Specialist:

* Feature-RAG: ``configs/experiments/fixtures/agent-load-request.json`` (feature_lookup,
  fraud_scoring, knowledge_search) must return ``status ok``, this request's ``request_id``,
  features, citations, ``unavailable []`` and ``fraud_probability`` equal to the pinned answer;
* Drift: ``configs/experiments/fixtures/agent-load-drift-request.json`` must return the pinned PSI.

A task counts only when it reaches ``completed`` and its artifact is a ``g3-orchestration/v1``
SpecialistEvidence object with no unknown fields; HTTP 200 alone is never a pass. Each verdict names
the gate that decided it, in order: ``TRANSPORT`` (no HTTP exchange), ``PROTOCOL`` (JSON-RPC error,
non-terminal or failed task, timeout), ``SCHEMA`` (no or invalid SpecialistEvidence), ``BUSINESS``
(status, this request's id, features/citations/drift, prompt revision), ``REGRESSION`` (the pinned
numbers), then ``PASS``.

Numerical tolerance: the pinned values are float64 results of a deterministic computation on fixed
inputs (one model version on fixed features; PSI over fixed reference and current snapshots), and they
reproduced bit-for-bit in every recorded run (2026-10-31, 2026-11-02, 2026-11-16). A relative tolerance
of 1e-9 absorbs only platform-level floating-point noise (BLAS or CPU differences of a few ulps) and is
orders of magnitude below any business meaning (a probability or PSI change in the 9th significant
digit). A different model or baseline is a deliberate change that must update the contract. The
candidate runs before any production mutation, so a regression here stops the release; it never
triggers a rollback. The post-promotion business E2E is the frozen replay verdict, which checks
validity, not exact numbers. Endpoints are
allowlisted (loopback port-forward only), so a typo can never send a request elsewhere. Each
verdict records the environment, endpoint, agent, SHA, image and the declared side effects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_FIELDS = frozenset({"status", "request_id", "features", "fraud_score", "drift", "citations", "unavailable",
                             "contract_version", "prompt_revision"})
CONTRACT_VERSION = "g3-orchestration/v1"
TERMINAL = frozenset({"completed", "failed", "canceled", "rejected", "unknown"})
LOOPBACK = re.compile(r"^http://127\.0\.0\.1:([0-9]{2,5})/?$")
AGENT = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
RELATIVE_TOLERANCE = 1e-9
ABSOLUTE_TOLERANCE = 1e-15  # only matters for an expected value of exactly 0
Post = Callable[[str, Mapping[str, Any], Mapping[str, str], float], Mapping[str, Any]]
SIDE_EFFECTS = ("one A2A session actor per request on the shared WorkerPool (suspended after the reply)",
                "read-only MCP tool calls (feature_lookup, fraud_scoring, knowledge_search | drift_detection)",
                "MCP and kagent audit log lines")


class EndpointError(ValueError):
    """The endpoint is outside the allowlist."""


@dataclass
class SpecialistVerdict:
    agent: str
    target: str
    verdict: str
    request_id: str
    endpoint: str
    environment: str
    git_sha: str
    image: str
    state: str | None = None
    gate: str = "TRANSPORT"
    reasons: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float | None = None
    side_effects: tuple[str, ...] = SIDE_EFFECTS


def sandbox_url(endpoint: str, namespace: str, agent: str) -> str:
    """The kagent SandboxAgent route; only a loopback port-forward is accepted as the endpoint."""

    if not LOOPBACK.fullmatch(endpoint) or not 1024 <= int(LOOPBACK.fullmatch(endpoint).group(1)) <= 65535:
        raise EndpointError(f"endpoint {endpoint!r} is not an allowed loopback port-forward (http://127.0.0.1:<port>/)")
    if not AGENT.fullmatch(namespace) or not AGENT.fullmatch(agent):
        raise EndpointError("namespace and agent must be DNS labels")
    url = f"{endpoint.rstrip('/')}/api/a2a-sandboxes/{namespace}/{agent}/"
    if urlsplit(url).hostname != "127.0.0.1":
        raise EndpointError("the SandboxAgent route must stay on the loopback port-forward")
    return url


def http_post(url: str, message: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> Mapping[str, Any]:
    request = Request(url, data=json.dumps(message).encode(), method="POST",
                      headers={"Content-Type": "application/json", **headers})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def payload(fixture: Path, request_id: str) -> dict[str, Any]:
    """The fixture's ``message/send`` for one request, with its own session.

    kagent routes a SandboxAgent message to a session actor by ``contextId`` and refuses a message without it
    ("contextId (session id) is required for substrate sandbox agents"). The context is the request id, as the
    Coordinator does for its Specialist calls (programmatic_coordinator.call_for): one fresh session per request,
    never shared between checks or executions. ``tasks/get`` polls by task id and needs no context.
    """

    text = fixture.read_text(encoding="utf-8")
    if "__TASK_ID__" not in text:
        raise ValueError(f"fixture {fixture} has no __TASK_ID__ placeholder")
    body = json.loads(text.replace("__TASK_ID__", request_id))
    body["params"]["message"]["contextId"] = request_id
    return body


def run_task(post: Post, url: str, body: Mapping[str, Any], headers: Mapping[str, str], *, timeout: float,
             clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> tuple[dict, float]:
    """Send one task and poll it to a terminal state; returns (last JSON-RPC reply, seconds)."""

    started = clock()
    reply = dict(post(url, body, headers, timeout))
    deadline = started + timeout
    while True:
        result = reply.get("result") if isinstance(reply.get("result"), dict) else None
        state = ((result or {}).get("status") or {}).get("state")
        if result is None or state in TERMINAL or not result.get("id"):
            return reply, clock() - started
        if clock() >= deadline:
            reply["timeout"] = True
            return reply, clock() - started
        sleep(2)
        reply = dict(post(url, {"jsonrpc": "2.0", "id": body.get("id"), "method": "tasks/get",
                                "params": {"id": result["id"]}}, headers, timeout))


def evidence_of(reply: Mapping[str, Any]) -> dict[str, Any] | None:
    from ops.llm_agent.replay_verdict import structured_result  # stdlib-only, frozen helper

    return structured_result(dict(reply))


def _close(value: Any, expected: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isclose(
        float(value), expected, rel_tol=RELATIVE_TOLERANCE, abs_tol=ABSOLUTE_TOLERANCE)


def classify(reply: Mapping[str, Any], request_id: str,
             expect: Mapping[str, Any]) -> tuple[str, str, list[str], dict]:
    """(verdict, gate, reasons, observed): PASS only for a completed task whose SpecialistEvidence matches."""

    observed: dict[str, Any] = {}
    if reply.get("timeout"):
        return "A2A_TIMEOUT", "PROTOCOL", ["the task did not reach a terminal state in time"], observed
    if "error" in reply or not isinstance(reply.get("result"), dict):
        return "FAILED_TASK", "PROTOCOL", [f"JSON-RPC error or no result: {json.dumps(reply.get('error'))[:300]}"], observed
    state = (reply["result"].get("status") or {}).get("state")
    observed["state"] = state
    if state != "completed":
        return "FAILED_TASK", "PROTOCOL", [f"task state {state} (expected completed)"], observed
    evidence = evidence_of(reply)
    if evidence is None:
        return "INVALID_VERDICT", "SCHEMA", ["no SpecialistEvidence artifact (contract_version) in the task"], observed
    observed.update({k: evidence.get(k) for k in ("status", "request_id", "contract_version", "unavailable",
                                                  "prompt_revision")})
    unknown = set(evidence) - EVIDENCE_FIELDS
    if unknown:
        return "INVALID_VERDICT", "SCHEMA", [f"unknown evidence fields {sorted(unknown)}"], observed
    if evidence.get("contract_version") != CONTRACT_VERSION:
        return "INVALID_VERDICT", "SCHEMA", [f"contract {evidence.get('contract_version')} (expected {CONTRACT_VERSION})"], observed
    if evidence.get("status") in {"partial", "unavailable"} or evidence.get("unavailable"):
        return "PARTIAL", "BUSINESS", [f"status {evidence.get('status')}; unavailable={evidence.get('unavailable')}"], observed
    reasons = []
    if evidence.get("request_id") != request_id:
        # A stale or replayed answer (another execution's request) never counts.
        reasons.append(f"request_id {evidence.get('request_id')} is not this request's {request_id}")
    if evidence.get("status") != "ok":
        reasons.append(f"status {evidence.get('status')} (expected ok)")
    if not evidence.get("prompt_revision"):
        reasons.append("no prompt_revision")
    elif "prompt_revision" in expect and evidence["prompt_revision"] != expect["prompt_revision"]:
        # Identity of the code-pinned prompt, not Prompt Registry consumption (frozen application).
        reasons.append(f"prompt_revision {evidence['prompt_revision']} is not {expect['prompt_revision']}")
    for key in ("features", "citations", "drift"):
        if key in expect and bool(evidence.get(key)) != bool(expect[key]):
            reasons.append(f"{key} {'missing' if expect[key] else 'unexpected'}")
    if reasons:
        return "BUSINESS_MISMATCH", "BUSINESS", reasons, observed
    if "fraud_probability" in expect:
        value = (evidence.get("fraud_score") or {}).get("fraud_probability")
        observed["fraud_probability"] = value
        if not _close(value, expect["fraud_probability"]):
            reasons.append(f"fraud_probability {value} is not the pinned {expect['fraud_probability']} (rel 1e-9)")
    if "psi" in expect:
        drift = evidence.get("drift") or [{}]
        value = drift[0].get("psi") if isinstance(drift[0], dict) else None
        observed["psi"] = value
        if not _close(value, expect["psi"]):
            reasons.append(f"psi {value} is not the pinned {expect['psi']} (rel 1e-9)")
    if reasons:
        return "KNOWN_ANSWER_MISMATCH", "REGRESSION", reasons, observed
    return "PASS", "PASS", [], observed


def verify(*, contract: Mapping[str, Any], agent: str, target: str, endpoint: str, environment: str, git_sha: str,
           image: str, request_id: str, post: Post = http_post, timeout: float | None = None,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> SpecialistVerdict:
    """Verify ``target`` (the candidate or production SandboxAgent) with ``agent``'s fixture and answers."""

    spec = contract["agents"][agent]
    url = sandbox_url(endpoint, contract["namespace"], target)
    headers = {"X-User-Id": contract["kagent"]["user_id"]}
    result = SpecialistVerdict(agent, target, "FAILED_TASK", request_id, url, environment, git_sha, image)
    try:
        reply, elapsed = run_task(post, url, payload(ROOT / spec["fixture"], request_id), headers,
                                  timeout=timeout or contract["timeouts"]["task_seconds"], clock=clock, sleep=sleep)
    except (HTTPError, URLError, OSError, ValueError) as error:
        result.verdict, result.gate, result.reasons = "TRANSPORT_ERROR", "TRANSPORT", [type(error).__name__]
        return result
    result.elapsed_seconds = round(elapsed, 3)
    result.verdict, result.gate, result.reasons, result.observed = classify(reply, request_id, spec["expect"])
    result.state = result.observed.get("state")
    return result


def to_json(verdict: SpecialistVerdict) -> dict[str, Any]:
    data = asdict(verdict)
    data["side_effects"] = list(verdict.side_effects)
    return data
