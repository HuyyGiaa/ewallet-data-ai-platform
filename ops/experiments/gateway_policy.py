"""Bounded agentgateway policy probe for the ewallet-bench stack.

Tests only policies the benchmark gateway really configures
(``infra/kubernetes/experiments/vllm-bench.yaml.tmpl``):

* authentication: ``apiKey`` strict mode with one client key; the vLLM backend key is a
  different Secret and must not work as a client key;
* request rate limit: ``localRateLimit`` of 12 requests per second per model, so a burst
  of 16 concurrent one-token requests must see some HTTP 429;
* routing: every accepted response names the route under test in ``x-ewallet-backend``,
  and in the llm-d arm the EPP's own request counter must grow by at least the number of
  accepted requests, so a request that bypassed the router would show.

It sends at most 1 + 1 + 1 + 1 + 3 + burst requests, each asking for one token, and runs
outside the measured windows. Keys come from the environment and are never written or
printed; the report records only status codes, headers and short error bodies.

usage: EWALLET_GATEWAY_KEY=... [EWALLET_BACKEND_KEY=...] python -m ops.experiments.gateway_policy \
    --gateway http://127.0.0.1:13300 --expect-backend primary [--epp-metrics http://127.0.0.1:13302/metrics] \
    --output <dir>/gateway-policy/results.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BODY = {"model": "ewallet-fraud-llm", "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1, "temperature": 0}
REJECTED = (401, 403)
KEEP_HEADERS = ("x-ewallet-backend", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                "x-ratelimit-reset", "content-type")

Send = Callable[[str | None], dict[str, Any]]


def sender(gateway: str, timeout: float = 120.0) -> Send:
    """Return a function that posts one tiny chat completion with an optional bearer key."""

    url = gateway.rstrip("/") + "/v1/chat/completions"

    def send(key: str | None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(url, data=json.dumps(BODY).encode(), method="POST", headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 (local port-forward only)
                response.read()
                return {"status": response.status,
                        "headers": {k: v for k, v in response.headers.items() if k.lower() in KEEP_HEADERS}}
        except HTTPError as error:
            body = error.read().decode(errors="replace")[:200]
            return {"status": error.code, "body": body,
                    "headers": {k: v for k, v in error.headers.items() if k.lower() in KEEP_HEADERS}}
        except (URLError, OSError) as error:
            return {"status": None, "error": f"{type(error).__name__}: {error}"[:200]}

    return send


def epp_requests(fetch: Callable[[], str]) -> float | None:
    """Sum of ``llm_d_epp_request_total`` over all series, or None when unreadable."""

    try:
        text = fetch()
    except (URLError, OSError, ValueError):
        return None
    total, seen = 0.0, False
    for line in text.splitlines():
        if line.startswith("llm_d_epp_request_total"):
            total += float(line.rsplit(" ", 1)[1])
            seen = True
    return total if seen else None


def _backend(result: dict[str, Any]) -> str | None:
    return {k.lower(): v for k, v in result.get("headers", {}).items()}.get("x-ewallet-backend")


def probe(send: Send, *, client_key: str, backend_key: str | None, expect_backend: str, burst: int,
          epp: Callable[[], str] | None) -> dict[str, Any]:
    """Run every case once and return the report."""

    cases: list[dict[str, Any]] = []

    def case(name: str, policy: str, expected: str, result: dict[str, Any], passed: bool) -> None:
        cases.append({"case": name, "policy": policy, "expected": expected, "passed": passed,
                      "status": result.get("status"), "backend": _backend(result),
                      **{k: result[k] for k in ("body", "error") if k in result}})

    valid = send(client_key)
    case("valid_client_key", "auth", "200", valid, valid.get("status") == 200)
    missing = send(None)
    case("missing_key", "auth", "401 or 403", missing, missing.get("status") in REJECTED)
    wrong = send(secrets.token_hex(32))
    case("wrong_key", "auth", "401 or 403", wrong, wrong.get("status") in REJECTED)
    if backend_key:
        backend = send(backend_key)
        case("backend_key_as_client_key", "auth", "401 or 403", backend, backend.get("status") in REJECTED)
    else:
        cases.append({"case": "backend_key_as_client_key", "policy": "auth", "passed": None,
                      "status": "NOT_EXECUTED", "reason": "backend key not provided"})

    before = epp_requests(epp) if epp else None
    routed = [send(client_key) for _ in range(3)]
    after = epp_requests(epp) if epp else None
    backends = sorted({str(_backend(r)) for r in routed})
    ok = all(r.get("status") == 200 for r in routed) and backends == [expect_backend]
    routing: dict[str, Any] = {"case": "routing_backend_header", "policy": "routing", "expected": expect_backend,
                               "passed": ok, "statuses": [r.get("status") for r in routed], "backends": backends}
    if epp:
        grew = None if before is None or after is None else after - before
        routing["epp_request_total_delta"] = grew
        routing["passed"] = ok and grew is not None and grew >= 3
    cases.append(routing)

    with ThreadPoolExecutor(max_workers=burst) as pool:
        results = list(pool.map(lambda _: send(client_key), range(burst)))
    counts: dict[str, int] = {}
    for result in results:
        counts[str(result.get("status"))] = counts.get(str(result.get("status")), 0) + 1
    limited = next((r for r in results if r.get("status") == 429), {})
    cases.append({"case": "rate_limit_burst", "policy": "localRateLimit 12 requests/s per model",
                  "expected": f"{burst} concurrent: some 200 and some 429", "burst": burst, "status_counts": counts,
                  "passed": counts.get("200", 0) >= 1 and counts.get("429", 0) >= 1,
                  "example_429": {k: limited[k] for k in ("body", "headers") if k in limited}})
    executed = [c for c in cases if c.get("passed") is not None]
    return {"schema": "ewallet-bench-gateway-policy-v1", "at": datetime.now(timezone.utc).isoformat(),
            "expect_backend": expect_backend, "cases": cases,
            "verdict": "PASS" if executed and all(c["passed"] for c in executed) else "FAIL"}


def main(argv: Sequence[str] | None = None) -> int:
    """Write the report; exit 1 when any executed case failed."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--expect-backend", required=True, choices=("primary", "llm-d"))
    parser.add_argument("--epp-metrics")
    parser.add_argument("--burst", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    for url in filter(None, (args.gateway, args.epp_metrics)):
        if not url.startswith("http://127.0.0.1:"):
            parser.error("local port-forwards only: http://127.0.0.1:<port>")
    if not 13 <= args.burst <= 24:
        parser.error("--burst must be 13-24: above the 12/s limit, small enough not to flood")
    client_key = os.environ.get("EWALLET_GATEWAY_KEY") or parser.error("EWALLET_GATEWAY_KEY missing")

    def fetch_epp() -> str:
        with urlopen(args.epp_metrics, timeout=20) as response:  # noqa: S310 (local port-forward only)
            return response.read().decode()

    report = probe(sender(args.gateway), client_key=client_key, backend_key=os.environ.get("EWALLET_BACKEND_KEY"),
                   expect_backend=args.expect_backend, burst=args.burst,
                   epp=fetch_epp if args.epp_metrics else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in report["cases"]:
        print(f"{item['case']:<28} status={item.get('status', item.get('status_counts'))} passed={item.get('passed')}")
    print(f"gateway policy verdict: {report['verdict']}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
