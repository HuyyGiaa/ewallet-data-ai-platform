"""Run one fixed task suite on both agent variants and compare them case by case.

Each case goes through the production ``plan`` path: PII screen, the real gateway call,
then grounding. Both variants see every case, in alternating order, so the comparison is
paired and neither variant gets an easier workload. Every case is reported, including
failures; nothing is dropped. The harness measures the Coordinator's LLM decision;
end-to-end tool execution is measured separately through A2A on the cluster.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import httpx

from ai_platform.llm_agent.agent_ab import AgentExperiment, AgentVariant
from ai_platform.llm_agent.agent_metrics import ab_outcome
from ai_platform.llm_agent.nl_intake import IntakeFailure, OpenAIToolPlanner, plan
from ops.experiments.ab_stats import percentile


class RedirectTransport(httpx.AsyncBaseTransport):
    """Send the approved in-cluster URL to a port-forward or a local gateway."""

    def __init__(self, target: str) -> None:
        self._target = httpx.URL(target)
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Rewrite scheme, host and port, and keep the rest of the request."""
        request.url = request.url.copy_with(
            scheme=self._target.scheme, host=self._target.host, port=self._target.port
        )
        return await self._inner.handle_async_request(request)


def planner_for(variant: AgentVariant, gateway_url: str, timeout_seconds: float) -> OpenAIToolPlanner:
    """Build the production planner for one variant."""
    return OpenAIToolPlanner(
        model=variant.model,
        temperature=variant.temperature,
        max_tokens=variant.max_tokens,
        variant=variant.name,
        timeout_seconds=timeout_seconds,
        transport=RedirectTransport(gateway_url),
    )


def grade(case: Mapping[str, Any], selection: Any | None, failure_code: str | None) -> dict[str, Any]:
    """Grade one result against the suite's expectation."""
    expected = case["expect"]
    if expected["tool"] is None:
        correct = failure_code == "unsupported_request"
        return {"correct_tool": correct, "task_success": correct, "tool_calls": int(selection is not None)}
    if selection is None:
        return {"correct_tool": False, "task_success": False, "tool_calls": 0}
    correct_tool = selection.name == expected["tool"]
    arguments = selection.arguments
    exact = all(arguments.get(key) == value for key, value in expected["arguments"].items())
    return {"correct_tool": correct_tool, "task_success": correct_tool and exact, "tool_calls": 1}


async def run_case(planner: OpenAIToolPlanner, case: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """Execute one case and return its full record."""
    started = time.perf_counter()
    selection = None
    code = None
    try:
        result = await plan(case["prompt"], planner=planner, context_id=f"{run_id}-{case['id']}", message_id="eval")
        selection = result.selection
    except IntakeFailure as failure:
        code = failure.code
    record: dict[str, Any] = {
        "id": case["id"], "category": case["category"], "variant": planner.variant,
        "latency_seconds": round(time.perf_counter() - started, 4),
        "failure_code": code, "outcome": ab_outcome(code),
        "prompt_tokens": selection.prompt_tokens if selection else 0,
        "completion_tokens": selection.completion_tokens if selection else 0,
        "served_model": selection.served_model if selection else "",
        "backend": selection.backend if selection else "",
    }
    return {**record, **grade(case, selection, code)}


def summarise_variant(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate one variant over the whole suite."""
    total = len(records)
    latencies = [row["latency_seconds"] for row in records]
    return {
        "cases": total,
        "task_success_rate": round(sum(row["task_success"] for row in records) / total, 4),
        "correct_tool_rate": round(sum(row["correct_tool"] for row in records) / total, 4),
        "invalid_tool_calls": sum(row["outcome"] == "invalid_tool" for row in records),
        "invalid_arguments": sum(row["outcome"] == "invalid_arguments" for row in records),
        "llm_errors": sum(row["outcome"] == "llm_error" for row in records),
        "tool_calls": sum(row["tool_calls"] for row in records),
        "latency_p50_seconds": percentile(latencies, 0.5),
        "latency_p95_seconds": percentile(latencies, 0.95),
        "prompt_tokens": sum(row["prompt_tokens"] for row in records),
        "completion_tokens": sum(row["completion_tokens"] for row in records),
        "served_models": dict(Counter(row["served_model"] or "none" for row in records)),
        "backends": dict(Counter(row["backend"] or "none" for row in records)),
        "failure_codes": dict(Counter(row["failure_code"] or "none" for row in records)),
    }


def compare(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count cases that only one variant solved, for a paired reading."""
    by_case: dict[str, dict[str, bool]] = {}
    for row in records:
        by_case.setdefault(row["id"], {})[row["variant"]] = row["task_success"]
    return {
        "both_solved": sum(all(values.values()) for values in by_case.values()),
        "neither_solved": sum(not any(values.values()) for values in by_case.values()),
        "only_baseline": sum(v["baseline"] and not v["candidate"] for v in by_case.values()),
        "only_candidate": sum(v["candidate"] and not v["baseline"] for v in by_case.values()),
    }


async def run_suite(
    planners: Mapping[str, OpenAIToolPlanner], cases: Sequence[Mapping[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    """Run each case on both variants, alternating which goes first."""
    records = []
    for index, case in enumerate(cases):
        order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for name in order:
            records.append(await run_case(planners[name], case, run_id))
    return records


def main() -> int:
    """Run the suite and write raw records and a summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="experiment JSON")
    parser.add_argument("--suite", type=Path, default=Path("configs/experiments/fixtures/agent-ab-suite.jsonl"))
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=190.0)
    parser.add_argument("--limit", type=int, help="run the first N cases only")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("EWALLET_LLM_API_KEY"):
        parser.error("EWALLET_LLM_API_KEY must hold the gateway client key")
    experiment = AgentExperiment.from_json(args.config.read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in args.suite.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        cases = cases[: args.limit]
    planners = {v.name: planner_for(v, args.gateway_url, args.timeout_seconds) for v in experiment.variants}
    records = asyncio.run(run_suite(planners, cases, experiment.experiment_id))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    summary = {
        "experiment": experiment.experiment_id,
        "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
        "cases": len(cases),
        "variants": {
            name: summarise_variant([row for row in records if row["variant"] == name]) for name in planners
        },
        "paired": compare(records),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
