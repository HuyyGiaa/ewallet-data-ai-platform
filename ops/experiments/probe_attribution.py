"""Attribute the gateway policy probe's non-policy statuses (e.g. HTTP 503) to a component.

The probe's burst sends 16 identical one-token requests; the gateway admits 12 and answers
the rest with 429. In the EXP-V2 smoke the llm-d arm then returned 4 x 503 that the
gateway log could only place "upstream of agentgateway". Two components can produce them:

* vLLM admission control: ``--max-num-queued-reqs 8`` caps a pod's in-flight requests
  (waiting + running) and answers the next one with 503, counting
  ``vllm:admission_rejections_total{reason="max_num_queued_reqs"}`` (vLLM 0.32.0 in the pinned
  image). Identical prompts make the EPP's prefix-cache scorer send the burst to one pod.
* the llm-d EPP, which counts its own failures in ``llm_d_epp_request_error_total``.

The runner snapshots both counters immediately before and after the probe (outside every
measured window). This module diffs the snapshots and names the component only when its
counter delta equals the 503 count; otherwise the result is ``UNATTRIBUTED``. Behind the
EPP, a vLLM rejection is also counted by the EPP as ``ModelServerError`` (it relays the
backend's 503); when both deltas equal the 503 count, the cause is vLLM admission control.

usage: python -m ops.experiments.probe_attribution --results results.json --snapshots <dir> --output attribution.json
Snapshot files: ``vllm-<pod>-before.prom`` / ``-after.prom`` and ``epp-before.prom`` / ``epp-after.prom``.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)')
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
ADMISSION = "vllm:admission_rejections_total"
SUCCESS = "vllm:request_success_total"
EPP_ERROR = "llm_d_epp_request_error_total"
RELAYED = "ModelServerError"


def parse(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Prometheus text format to ``{(name, sorted labels): value}``; comments are skipped."""

    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in text.splitlines():
        match = SAMPLE.match(line.strip())
        if not match or line.startswith("#"):
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        labels = tuple(sorted(LABEL.findall(match.group(2) or "")))
        samples[(match.group(1), labels)] = value
    return samples


def delta(before: str | None, after: str | None, name: str, key: str) -> dict[str, float] | None:
    """Increase of counter ``name`` per value of label ``key``; None when a snapshot is missing."""

    if before is None or after is None:
        return None
    old, new = parse(before), parse(after)
    out: dict[str, float] = {}
    for (metric, labels), value in new.items():
        if metric != name:
            continue
        group = dict(labels).get(key, "")
        out[group] = out.get(group, 0.0) + value - old.get((metric, labels), 0.0)
    return out


def _read(path: Path) -> str | None:
    """Snapshot text; None when the file is missing or empty (the read failed)."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


def attribute(results: Mapping[str, Any], snapshots: Path) -> dict[str, Any]:
    """Counter deltas around the probe and the component that explains its 503s."""

    burst = next((case for case in results.get("cases", []) if case.get("case") == "rate_limit_burst"), {})
    counts = {str(k): int(v) for k, v in (burst.get("status_counts") or {}).items()}
    unavailable = counts.get("503", 0)
    pods: dict[str, Any] = {}
    for before in sorted(snapshots.glob("vllm-*-before.prom")):
        pod = before.name[len("vllm-"):-len("-before.prom")]
        after = before.with_name(f"vllm-{pod}-after.prom")
        pods[pod] = {"admission_rejections": delta(_read(before), _read(after), ADMISSION, "reason"),
                     "request_success": delta(_read(before), _read(after), SUCCESS, "finished_reason")}
    epp = delta(_read(snapshots / "epp-before.prom"), _read(snapshots / "epp-after.prom"), EPP_ERROR, "error_code")
    vllm_rejected = sum(sum((item["admission_rejections"] or {}).values()) for item in pods.values())
    epp_errors = sum((epp or {}).values())
    readable = bool(pods) and all(item["admission_rejections"] is not None for item in pods.values())
    # Behind the EPP, vLLM's own 503 also counts once as an EPP ModelServerError: the EPP
    # relays the backend's answer. Those errors are the same requests, not a second cause.
    relayed = round((epp or {}).get(RELAYED, 0.0))
    only_relayed = round(epp_errors) == relayed
    rejecting = sorted(pod for pod, item in pods.items() if sum((item["admission_rejections"] or {}).values()))
    if unavailable == 0:
        verdict, component = "NO_503", None
    elif readable and round(vllm_rejected) == unavailable and round(epp_errors) == 0:
        verdict = "ATTRIBUTED"
        component = f"vLLM admission control (max_num_queued_reqs) on {', '.join(rejecting)}"
    elif readable and round(vllm_rejected) == unavailable and only_relayed and relayed == unavailable:
        verdict = "ATTRIBUTED"
        component = (f"vLLM admission control (max_num_queued_reqs) on {', '.join(rejecting)}, "
                     f"relayed by the llm-d EPP as {RELAYED}")
    elif epp is not None and round(epp_errors) == unavailable and round(vllm_rejected) == 0:
        verdict, component = "ATTRIBUTED", f"llm-d EPP request errors {sorted(k for k, v in epp.items() if v)}"
    else:
        verdict, component = "UNATTRIBUTED", None
    return {"schema": "ewallet-bench-probe-attribution-v1", "status_counts": counts, "http_503": unavailable,
            "vllm": pods, "vllm_admission_rejections_total": vllm_rejected,
            "epp_request_errors": epp, "epp_request_errors_total": epp_errors if epp is not None else None,
            "verdict": verdict, "component": component}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        results = json.loads(args.results.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        results = {}
    report = attribute(results, args.snapshots)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"probe 503s: {report['http_503']} -> {report['verdict']}"
          + (f": {report['component']}" if report["component"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
