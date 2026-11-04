"""Send one validated local-only A2A request to the BYO Specialist.

The script never reads credentials. Operators provide an explicit localhost
port-forward, so neither a bearer token nor a Kubernetes endpoint can be
accidentally written to evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_platform.llm_agent.business_request import business_request_from_a2a
from ai_platform.llm_agent.programmatic_coordinator import SpecialistEvidence


def _endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise argparse.ArgumentTypeError("endpoint must be http://127.0.0.1:<port>/")
    return value


def _terminal_artifact(response: dict[str, object]) -> SpecialistEvidence:
    result = response.get("result")
    if not isinstance(result, dict) or result.get("status", {}).get("state") != "completed":
        raise ValueError("Specialist did not return a completed A2A task")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError("Specialist must return exactly one terminal artifact")
    parts = artifacts[0].get("parts", [])
    if len(parts) != 1 or not isinstance(parts[0].get("text"), str):
        raise ValueError("Specialist terminal artifact is not one text part")
    evidence = SpecialistEvidence.model_validate_json(parts[0]["text"])
    if evidence.status != "ok":
        raise ValueError("Specialist evidence is not successful")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", type=_endpoint, default="http://127.0.0.1:18084/")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    payload = json.loads(args.request.read_text(encoding="utf-8"))
    expected = business_request_from_a2a(payload)
    started = time.monotonic()
    with httpx.Client(timeout=args.timeout) as client:
        http_response = client.post(args.endpoint, json=payload)
    elapsed = round(time.monotonic() - started, 3)
    decoded = http_response.json()
    evidence = _terminal_artifact(decoded)
    if evidence.request_id != expected.request_id:
        raise ValueError("Specialist changed request identity")

    args.response.parent.mkdir(parents=True, exist_ok=True)
    args.response.write_text(json.dumps(decoded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "elapsed_seconds": elapsed,
        "http_status": http_response.status_code,
        "request_id": evidence.request_id,
        "status": evidence.status,
        "feature_count": len(evidence.features),
        "has_fraud_score": evidence.fraud_score is not None,
        "citation_count": len(evidence.citations),
        "response": str(args.response),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
