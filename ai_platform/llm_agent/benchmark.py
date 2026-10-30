"""Fail-closed schema and aggregation for real LLM serving measurements."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import mean
from typing import Iterable


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile from no values")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


@dataclass(frozen=True)
class RequestMeasurement:
    request_id: str
    backend: str
    model: str
    prompt_revision: str
    e2e_ms: float
    ttft_ms: float
    inter_token_latencies_ms: tuple[float, ...]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    error: bool
    fallback: bool

    def __post_init__(self) -> None:
        if not self.request_id or not self.backend or not self.model or not self.prompt_revision:
            raise ValueError("measurement identity is incomplete")
        numeric = (self.e2e_ms, self.ttft_ms, *self.inter_token_latencies_ms)
        if any(not math.isfinite(value) or value < 0 for value in numeric):
            raise ValueError("latencies must be finite and non-negative")
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if not self.error and self.output_tokens == 0:
            raise ValueError("successful measurements require output tokens")


@dataclass(frozen=True)
class ResourceMeasurement:
    cpu_millicores: float
    memory_mib: float

    def __post_init__(self) -> None:
        if self.cpu_millicores < 0 or self.memory_mib <= 0:
            raise ValueError("resource measurements must be positive")


def summarize(
    measurements: Iterable[RequestMeasurement],
    resources: Iterable[ResourceMeasurement],
    *,
    wall_seconds: float,
) -> dict[str, object]:
    rows = list(measurements)
    resource_rows = list(resources)
    if not rows or not resource_rows or wall_seconds <= 0:
        raise ValueError("actual request, resource and wall-clock measurements are required")
    latencies = [row.e2e_ms for row in rows]
    ttft = [row.ttft_ms for row in rows]
    itl = [value for row in rows for value in row.inter_token_latencies_ms]
    successes = [row for row in rows if not row.error]
    output_tokens = sum(row.output_tokens for row in successes)
    return {
        "measurement_kind": "actual",
        "request_count": len(rows),
        "success_count": len(successes),
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "ttft_ms": {
            "p50": percentile(ttft, 0.50),
            "p95": percentile(ttft, 0.95),
            "p99": percentile(ttft, 0.99),
        },
        "itl_ms": {
            "mean": mean(itl) if itl else None,
            "p95": percentile(itl, 0.95) if itl else None,
        },
        "tokens": {
            "input": sum(row.input_tokens for row in rows),
            "output": sum(row.output_tokens for row in rows),
            "total": sum(row.total_tokens for row in rows),
        },
        "throughput": {
            "requests_per_second": len(rows) / wall_seconds,
            "output_tokens_per_second": output_tokens / wall_seconds,
        },
        "resources": {
            "cpu_millicores_mean": mean(row.cpu_millicores for row in resource_rows),
            "cpu_millicores_peak": max(row.cpu_millicores for row in resource_rows),
            "memory_mib_mean": mean(row.memory_mib for row in resource_rows),
            "memory_mib_peak": max(row.memory_mib for row in resource_rows),
        },
        "error_rate": sum(row.error for row in rows) / len(rows),
        "fallback_rate": sum(row.fallback for row in rows) / len(rows),
        "measurements": [asdict(row) for row in rows],
    }
