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
SpecialistEvidence object with no unknown fields; HTTP 200 alone is never a pass. Endpoints are
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
TOLERANCE = 1e-12
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
    text = fixture.read_text(encoding="utf-8")
    if "__TASK_ID__" not in text:
        raise ValueError(f"fixture {fixture} has no __TASK_ID__ placeholder")
    return json.loads(text.replace("__TASK_ID__", request_id))


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
        float(value), expected, rel_tol=0, abs_tol=TOLERANCE)


def classify(reply: Mapping[str, Any], request_id: str, expect: Mapping[str, Any]) -> tuple[str, list[str], dict]:
    """PASS only for a completed task with a valid SpecialistEvidence artifact matching the known answers."""

    observed: dict[str, Any] = {}
    if reply.get("timeout"):
        return "A2A_TIMEOUT", ["the task did not reach a terminal state in time"], observed
    if "error" in reply or not isinstance(reply.get("result"), dict):
        return "FAILED_TASK", [f"JSON-RPC error or no result: {json.dumps(reply.get('error'))[:300]}"], observed
    state = (reply["result"].get("status") or {}).get("state")
    observed["state"] = state
    if state != "completed":
        return "FAILED_TASK", [f"task state {state} (expected completed)"], observed
    evidence = evidence_of(reply)
    if evidence is None:
        return "INVALID_VERDICT", ["no SpecialistEvidence artifact (contract_version) in the task"], observed
    unknown = set(evidence) - EVIDENCE_FIELDS
    observed.update({k: evidence.get(k) for k in ("status", "request_id", "contract_version", "unavailable",
                                                  "prompt_revision")})
    reasons = []
    if unknown:
        reasons.append(f"unknown evidence fields {sorted(unknown)}")
    if evidence.get("contract_version") != CONTRACT_VERSION:
        return "INVALID_VERDICT", [f"contract {evidence.get('contract_version')} (expected {CONTRACT_VERSION})"], observed
    if evidence.get("status") in {"partial", "unavailable"} or evidence.get("unavailable"):
        return "PARTIAL", [f"status {evidence.get('status')}; unavailable={evidence.get('unavailable')}"], observed
    if evidence.get("status") != "ok":
        reasons.append(f"status {evidence.get('status')} (expected ok)")
    if evidence.get("request_id") != request_id:
        reasons.append(f"request_id {evidence.get('request_id')} is not this request's {request_id}")
    if not evidence.get("prompt_revision"):
        reasons.append("no prompt_revision")
    elif "prompt_revision" in expect and evidence["prompt_revision"] != expect["prompt_revision"]:
        # Identity of the code-pinned prompt, not Prompt Registry consumption (frozen application).
        reasons.append(f"prompt_revision {evidence['prompt_revision']} is not {expect['prompt_revision']}")
    for key in ("features", "citations", "drift"):
        if key in expect and bool(evidence.get(key)) != bool(expect[key]):
            reasons.append(f"{key} {'missing' if expect[key] else 'unexpected'}")
    if "fraud_probability" in expect:
        value = (evidence.get("fraud_score") or {}).get("fraud_probability")
        observed["fraud_probability"] = value
        if not _close(value, expect["fraud_probability"]):
            reasons.append(f"fraud_probability {value} is not the pinned {expect['fraud_probability']}")
    if "psi" in expect:
        drift = evidence.get("drift") or [{}]
        value = drift[0].get("psi") if isinstance(drift[0], dict) else None
        observed["psi"] = value
        if not _close(value, expect["psi"]):
            reasons.append(f"psi {value} is not the pinned {expect['psi']}")
    return ("PASS" if not reasons else "KNOWN_ANSWER_MISMATCH"), reasons, observed


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
        result.verdict, result.reasons = "TRANSPORT_ERROR", [type(error).__name__]
        return result
    result.elapsed_seconds = round(elapsed, 3)
    result.verdict, result.reasons, result.observed = classify(reply, request_id, spec["expect"])
    result.state = result.observed.get("state")
    return result


def to_json(verdict: SpecialistVerdict) -> dict[str, Any]:
    data = asdict(verdict)
    data["side_effects"] = list(verdict.side_effects)
    return data
