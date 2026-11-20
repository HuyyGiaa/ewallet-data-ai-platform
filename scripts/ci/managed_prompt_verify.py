"""Managed Prompt runtime verdict for the staging profile (V2-P3B, rubric row 34).

Proves Prompt Registry -> validated ConfigMap -> BYO Coordinator -> real LLM request from three read-only
captures of one staging run (values-staging.yaml + values-staging-managed-prompt.yaml):

* ``--configmap``: ``kubectl -n ewallet-agents-staging get cm fraud-coordinator-intake-prompt-managed -o json``
  (written by ops/llm_agent/prompt_registry.py from the Registry);
* ``--coordinator-log``: ``kubectl -n ewallet-agents-staging logs deploy/fraud-coordinator`` (all pods);
* ``--answer``: the A2A JSON-RPC response to one natural-language request.

PASS needs every check: the ConfigMap holds the pinned prompt (version and SHA-256 from the chart's prompt
manifest, recomputed over system.md, source ``registry``); the Coordinator logged ``nl_prompt.resolved`` with
that version, SHA-256 and source ``registry`` and never a different prompt; it sent ``POST /v1/chat/completions``
to the STAGING agentgateway and got HTTP 200 (a production gateway URL fails the run); and the answer is the
nl-intake contract with an LLM tool selection that used tokens. Nothing is written to the cluster; the prompt
body is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "infra/helm/llm-agent-platform/prompts/manifest.json"
PROMPT = "fraud-coordinator-intake/v1"
STAGING_GATEWAY = "http://agentgateway.ewallet-agents-staging.svc.cluster.local:3000/v1/chat/completions"
LLM_REQUEST = re.compile(r'HTTP Request: POST (\S+/v1/chat/completions) "HTTP/1\.1 (\d{3})')
SELECTION = re.compile(r"LLM chọn công cụ `([a-z0-9-]+)` \((\d+)\+(\d+) token\) · (nl-intake/v\d+)")
VERDICT = "MANAGED_PROMPT_RUNTIME_VERIFIED"


def pinned(manifest: dict[str, Any], prompt: str = PROMPT) -> str:
    """The SHA-256 the chart pins for ``prompt`` (agent/version) in prompts/manifest.json."""

    agent, version = prompt.split("/", 1)
    entry = (manifest.get("prompts") or {}).get(prompt) or {}
    if entry.get("agent") != agent or entry.get("version") != version or not re.fullmatch(r"[0-9a-f]{64}",
                                                                                            str(entry.get("sha256"))):
        raise ValueError(f"{prompt} is not pinned in the prompt manifest")
    return str(entry["sha256"])


def _resolved_events(log: str) -> list[dict[str, Any]]:
    events = []
    for line in log.splitlines():
        start = line.find('{"event": "nl_prompt.resolved"')
        if start != -1:
            try:
                events.append(json.loads(line[start:]))
            except ValueError:
                events.append({"event": "nl_prompt.resolved", "unparsable": True})
    return events


def _answer_text(answer: dict[str, Any]) -> str:
    result = answer.get("result") or {}
    texts = [part.get("text", "") for artifact in result.get("artifacts") or []
             for part in artifact.get("parts") or [] if isinstance(part, dict)]
    return "\n".join(texts)


def evaluate(configmap: dict[str, Any], coordinator_log: str, answer: dict[str, Any], expected_sha: str,
             prompt: str = PROMPT) -> dict[str, Any]:
    checks: dict[str, str] = {}
    data = configmap.get("data") or {}
    meta = json.loads(data.get("prompt.json") or "{}")
    body_sha = hashlib.sha256((data.get("system.md") or "").encode("utf-8")).hexdigest()
    checks["configmap_prompt"] = "PASS" if (
        f"{meta.get('agent')}/{meta.get('version')}" == prompt and meta.get("source") == "registry"
        and meta.get("sha256") == expected_sha == body_sha
        and (configmap.get("metadata") or {}).get("namespace") == "ewallet-agents-staging") else "FAIL"

    events = _resolved_events(coordinator_log)
    good = [e for e in events if e.get("prompt") == prompt and e.get("sha256") == expected_sha
            and e.get("source") == "registry"]
    checks["coordinator_resolved_registry_prompt"] = "PASS" if good and len(good) == len(events) else "FAIL"

    requests = LLM_REQUEST.findall(coordinator_log)
    checks["llm_request_to_staging_gateway"] = "PASS" if requests and all(
        url == STAGING_GATEWAY for url, _ in requests) and any(code == "200" for _, code in requests) else "FAIL"

    selection = SELECTION.search(_answer_text(answer))
    checks["answer_used_llm_selection"] = "PASS" if selection and int(selection.group(2)) > 0 and int(
        selection.group(3)) > 0 and selection.group(4) == "nl-intake/v1" else "FAIL"

    passed = all(value == "PASS" for value in checks.values())
    return {"verdict": VERDICT if passed else "MANAGED_PROMPT_RUNTIME_NOT_VERIFIED", "prompt": prompt,
            "sha256": expected_sha, "checks": checks, "llm_requests": len(requests),
            "tool": selection.group(1) if selection else None,
            "tokens": [int(selection.group(2)), int(selection.group(3))] if selection else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--configmap", type=Path, required=True)
    parser.add_argument("--coordinator-log", type=Path, required=True)
    parser.add_argument("--answer", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="write the verdict here (create-only)")
    args = parser.parse_args(argv)
    expected = pinned(json.loads(MANIFEST.read_text(encoding="utf-8")))
    verdict = evaluate(json.loads(args.configmap.read_text(encoding="utf-8")),
                       args.coordinator_log.read_text(encoding="utf-8", errors="replace"),
                       json.loads(args.answer.read_text(encoding="utf-8")), expected)
    text = json.dumps(verdict, indent=2, sort_keys=True)
    if args.out:
        if args.out.exists():
            parser.error(f"{args.out} exists; evidence files are create-only")
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if verdict["verdict"] == VERDICT else 1


if __name__ == "__main__":
    sys.exit(main())
