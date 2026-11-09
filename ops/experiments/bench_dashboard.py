"""The EXP-V1 / EXP-V2 Grafana dashboard, defined once in code.

The same panel queries feed two consumers:

* the Grafana dashboard ConfigMap rendered into ``ewallet-bench`` (``dashboard()``);
* ``prom_readback``, which runs every query against Prometheus for a run's window and
  records which panels returned data (``queries()``).

So a query that is wrong or names a metric that does not exist shows up in the run's
evidence as an empty panel, not only on a screenshot.

Labels come from the benchmark Prometheus relabelling (``render_bench.py``): ``job`` is one
of vllm, agentgateway, epp, pod-usage; vLLM and gateway series also carry ``pod``,
``node``, ``run_id`` (for example ``baseline-01``) and ``arm``. Every series is from
``ewallet-bench``: that Prometheus discovers pods in that namespace only.

Server-side percentiles are interpolated from histogram buckets over a 1-minute rate at a
5-second scrape. The benchmark client's raw request records stay the authoritative numbers.
"""

from __future__ import annotations

import json
from typing import Any

UID = "edai2-llm-cpu-bench"
TITLE = "EDAI-2 — LLM CPU Benchmark (EXP-V1 / EXP-V2)"
DATASOURCE = {"type": "prometheus", "uid": "bench-prom"}
NO_VALUE = "no data: series absent"
VLLM = 'job="vllm",run_id=~"$run_id"'

# (group, title, unit, [(legend, expr)], panel type)
PANELS: list[tuple[str, str, str, list[tuple[str, str]], str]] = [
    ("A. Serving performance", "Output tokens/s per vLLM pod", "short", [
        ("{{pod}} {{run_id}}", f"sum by (pod, run_id) (rate(vllm:generation_tokens_total{{{VLLM}}}[1m]))")],
     "timeseries"),
    ("A. Serving performance", "Prompt tokens/s per vLLM pod", "short", [
        ("{{pod}} {{run_id}}", f"sum by (pod, run_id) (rate(vllm:prompt_tokens_total{{{VLLM}}}[1m]))")],
     "timeseries"),
    ("A. Serving performance", "TTFT p50 / p95 (vLLM histogram)", "s", [
        ("p50 {{run_id}}", "histogram_quantile(0.50, sum by (le, run_id) "
                           f"(rate(vllm:time_to_first_token_seconds_bucket{{{VLLM}}}[1m])))"),
        ("p95 {{run_id}}", "histogram_quantile(0.95, sum by (le, run_id) "
                           f"(rate(vllm:time_to_first_token_seconds_bucket{{{VLLM}}}[1m])))")], "timeseries"),
    ("A. Serving performance", "End-to-end latency p50 / p95 (vLLM histogram)", "s", [
        ("p50 {{run_id}}", "histogram_quantile(0.50, sum by (le, run_id) "
                           f"(rate(vllm:e2e_request_latency_seconds_bucket{{{VLLM}}}[1m])))"),
        ("p95 {{run_id}}", "histogram_quantile(0.95, sum by (le, run_id) "
                           f"(rate(vllm:e2e_request_latency_seconds_bucket{{{VLLM}}}[1m])))")], "timeseries"),
    ("A. Serving performance", "Running and waiting requests per pod", "short", [
        ("running {{pod}}", f"sum by (pod) (vllm:num_requests_running{{{VLLM}}})"),
        ("waiting {{pod}}", f"sum by (pod) (vllm:num_requests_waiting{{{VLLM}}})")], "timeseries"),
    ("A. Serving performance", "Gateway responses by HTTP status", "reqps", [
        ("{{status}}", 'sum by (status) (rate(agentgateway_requests_total{job="agentgateway"}[1m]))')],
     "timeseries"),
    # vLLM's --max-num-queued-reqs answers a request beyond a pod's in-flight cap with HTTP 503 and
    # counts it here; 0 across a measured window shows the cap did not shape the run.
    ("A. Serving performance", "vLLM admission rejections per pod (queue cap, HTTP 503)", "short", [
        ("{{pod}} {{reason}}", f"sum by (pod, reason) (increase(vllm:admission_rejections_total{{{VLLM}}}[1m]))")],
     "timeseries"),
    ("B. Resource utilization", "CPU usage per container (metrics-server)", "short", [
        ("{{pod}}/{{container}}", 'sum by (pod, container) (bench_pod_container_cpu_cores{job="pod-usage"})')],
     "timeseries"),
    ("B. Resource utilization", "Memory working set per container (metrics-server)", "bytes", [
        ("{{pod}}/{{container}}",
         'sum by (pod, container) (bench_pod_container_memory_working_set_bytes{job="pod-usage"})')], "timeseries"),
    ("B. Resource utilization", "Mean queue time per vLLM pod", "s", [
        ("{{pod}}", f"sum by (pod) (rate(vllm:request_queue_time_seconds_sum{{{VLLM}}}[1m])) / "
                    f"sum by (pod) (rate(vllm:request_queue_time_seconds_count{{{VLLM}}}[1m]))")], "timeseries"),
    ("B. Resource utilization", "Prefix-cache hit ratio per vLLM pod", "percentunit", [
        ("{{pod}}", f"sum by (pod) (rate(vllm:prefix_cache_hits_total{{{VLLM}}}[1m])) / "
                    f"sum by (pod) (rate(vllm:prefix_cache_queries_total{{{VLLM}}}[1m]))")], "timeseries"),
    ("C. Routing", "Completed requests per replica", "reqps", [
        ("{{pod}}", f"sum by (pod) (rate(vllm:request_success_total{{{VLLM}}}[1m]))")], "timeseries"),
    ("C. Routing", "Share of completed requests per replica (selected range)", "percentunit", [
        ("{{pod}}", f"sum by (pod) (increase(vllm:request_success_total{{{VLLM}}}[$__range])) / ignoring(pod) "
                    f"group_left sum(increase(vllm:request_success_total{{{VLLM}}}[$__range]))")], "bargauge"),
    ("C. Routing", "llm-d EPP ready endpoints", "short", [
        ("ready", 'max(llm_d_epp_ready_endpoints{job="epp"})')], "timeseries"),
    ("C. Routing", "llm-d EPP scheduling decisions per endpoint", "short", [
        ("{{endpoint_name}} {{status}}",
         'sum by (endpoint_name, status) (rate(llm_d_epp_scheduler_attempts_total{job="epp"}[1m]))')],
     "timeseries"),
    ("C. Routing", "llm-d EPP errors and metric-poll errors", "short", [
        ("request {{error_code}}", 'sum by (error_code) (rate(llm_d_epp_request_error_total{job="epp"}[1m]))'),
        ("metric poll", 'sum(rate(llm_d_epp_datalayer_poll_errors_total{job="epp"}[1m]))')], "timeseries"),
    ("C. Routing", "llm-d EPP scheduler latency p95", "s", [
        ("p95", "histogram_quantile(0.95, sum by (le) "
                '(rate(llm_d_epp_scheduler_e2e_duration_seconds_bucket{job="epp"}[1m])))')], "timeseries"),
    ("C. Routing", "Scrape health per target", "short", [
        ("{{job}} {{pod}}", "up"),
        ("pod-usage age (s)", 'time() - bench_pod_usage_last_success_timestamp_seconds{job="pod-usage"}')],
     "timeseries"),
]


def queries() -> list[dict[str, str]]:
    """Every panel query, with its panel title, for the Prometheus readback."""

    return [{"group": group, "panel": title, "legend": legend, "expr": expr}
            for group, title, _, targets, _ in PANELS for legend, expr in targets]


def _panel(index: int, y: int, x: int, title: str, unit: str, targets: list[tuple[str, str]], kind: str) -> dict:
    return {
        "id": index, "type": kind, "title": title, "datasource": DATASOURCE,
        "gridPos": {"h": 8, "w": 8, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "noValue": NO_VALUE}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]}}
        if kind == "timeseries" else {"displayMode": "basic", "orientation": "horizontal"},
        "targets": [{"refId": chr(65 + n), "datasource": DATASOURCE, "expr": expr, "legendFormat": legend,
                     **({"instant": True} if kind == "bargauge" else {})}
                    for n, (legend, expr) in enumerate(targets)],
    }


def dashboard() -> dict[str, Any]:
    """The Grafana dashboard model."""

    panels: list[dict[str, Any]] = [{
        "id": 1, "type": "text", "title": "About these panels", "gridPos": {"h": 3, "w": 24, "x": 0, "y": 0},
        "options": {"mode": "markdown", "content": (
            "Namespace **ewallet-bench** only (temporary benchmark stack, not production). "
            "`run_id` = arm-run, e.g. `baseline-01`. Server-side percentiles are histogram estimates; "
            "the benchmark client's raw records (`raw/run-NN/summary.json`) are authoritative. "
            "CPU/memory come from metrics-server via `metrics.k8s.io`. "
            "EPP panels have data only in the EXP-V2 optimized (llm-d) arm.")}}]
    y, column, group_seen, index = 3, 0, "", 2
    for group, title, unit, targets, kind in PANELS:
        if group != group_seen:
            if column:
                y, column = y + 8, 0
            panels.append({"id": index, "type": "row", "title": group, "collapsed": False,
                           "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}})
            index, y, group_seen = index + 1, y + 1, group
        panels.append(_panel(index, y, column * 8, title, unit, targets, kind))
        index += 1
        column = (column + 1) % 3
        if column == 0:
            y += 8
    return {
        "uid": UID, "title": TITLE, "schemaVersion": 39, "version": 1, "editable": False,
        "tags": ["edai2", "benchmark", "ewallet-bench"], "timezone": "utc", "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": [{
            # Display only: names the experiment on every screenshot; no query filters on it.
            "name": "experiment", "label": "experiment", "type": "query", "datasource": DATASOURCE,
            "query": {"query": "label_values(vllm:num_requests_running, experiment)", "refId": "experiment"},
            "definition": "label_values(vllm:num_requests_running, experiment)",
            "refresh": 2, "multi": False, "includeAll": False}, {
            "name": "run_id", "label": "run (arm-run)", "type": "query", "datasource": DATASOURCE,
            "query": {"query": "label_values(vllm:num_requests_running, run_id)", "refId": "run_id"},
            "definition": "label_values(vllm:num_requests_running, run_id)",
            "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"selected": True, "text": "All", "value": "$__all"}}]},
        "panels": panels,
    }


def dashboard_json() -> str:
    """Dashboard JSON as Grafana's file provisioner reads it."""

    return json.dumps(dashboard(), indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(dashboard_json(), end="")
