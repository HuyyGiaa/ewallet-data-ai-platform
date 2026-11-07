#!/usr/bin/env python3
"""Render the benchmark stack for one arm of EXP-V1 or EXP-V2.

The arm table below is the whole difference between the arms. Everything else comes from
one template, so an arm cannot drift by accident. Images must be pinned by digest.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra/kubernetes/experiments/vllm-bench.yaml.tmpl"
SERVICE_URL = "http://vllm-primary.ewallet-bench.svc.cluster.local:8000/v1"
ROUTER_URL = "http://llmd-router-epp.ewallet-bench.svc.cluster.local:8081/v1"

MODEL_06B = "/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca"
MODEL_17B = "/models/Qwen3-1.7B/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
GPU_ARM = {"THREAD_BIND": "unused", "REPLICAS": "1", "BACKEND_URL": SERVICE_URL,
           "BACKEND_LABEL": "primary", "TOKEN_BUDGET": "2000000"}
GPU_TOLERATIONS = """      tolerations:
        - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
        - {key: cloud.google.com/gke-spot, operator: Exists, effect: NoSchedule}
"""
CPU_ONLY_ENV = (
    '            - {name: VLLM_CPU_KVCACHE_SPACE, value: "2"}\n'
    '            - {name: VLLM_CPU_NUM_OF_RESERVED_CPU, value: "1"}\n'
    '            - {name: OMP_NUM_THREADS, value: "2"}\n'
    '            - {name: VLLM_CPU_OMP_THREADS_BIND, value: "__THREAD_BIND__"}\n'
)

ARMS: dict[str, dict[str, dict[str, str]]] = {
    "exp-v3a": {
        "baseline": {**GPU_ARM, "MODEL_PATH": MODEL_06B},
        "optimized": {**GPU_ARM, "MODEL_PATH": MODEL_17B},
    },
    "exp-v1": {
        "baseline": {"THREAD_BIND": "auto", "REPLICAS": "1", "BACKEND_URL": SERVICE_URL,
                     "BACKEND_LABEL": "primary", "TOKEN_BUDGET": "400000"},
        "optimized": {"THREAD_BIND": "0-1", "REPLICAS": "1", "BACKEND_URL": SERVICE_URL,
                      "BACKEND_LABEL": "primary", "TOKEN_BUDGET": "400000"},
    },
    "exp-v2": {
        "baseline": {"THREAD_BIND": "0-1", "REPLICAS": "2", "BACKEND_URL": SERVICE_URL,
                     "BACKEND_LABEL": "primary", "TOKEN_BUDGET": "150000"},
        "optimized": {"THREAD_BIND": "0-1", "REPLICAS": "2", "BACKEND_URL": ROUTER_URL,
                      "BACKEND_LABEL": "llm-d", "TOKEN_BUDGET": "150000"},
    },
}


def render(template: str, values: Mapping[str, str]) -> str:
    """Fill every placeholder.

    Raises:
        ValueError: A placeholder has no value or an image is not pinned by digest.
    """

    for key in ("VLLM_IMAGE", "GATEWAY_IMAGE"):
        if not re.search(r"@sha256:[0-9a-f]{64}$", values.get(key, "")):
            raise ValueError(f"{key} must be pinned by a full sha256 digest")
    text = template
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    missing = sorted(set(re.findall(r"__[A-Z_]+__", text)))
    if missing:
        raise ValueError(f"unfilled placeholders: {missing}")
    return text


def to_gpu(template: str, model_path: str) -> str:
    """Turn the CPU stack into the one-L4 stack, keeping every other setting equal.

    The served name stays the same in both arms, so the client and the gateway do not
    differ. Each edit must apply: a template change fails here, not on the cluster.
    """
    edits = (
        ("/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca", model_path),
        ('            - --enable-prefix-caching\n',
         '            - --enable-prefix-caching\n            - --gpu-memory-utilization\n            - "0.85"\n'),
        (CPU_ONLY_ENV, ""),
        ('            requests: {cpu: "2", memory: 6Gi}\n            limits: {cpu: "3", memory: 10Gi}\n',
         '            requests: {cpu: 2500m, memory: 10Gi, nvidia.com/gpu: 1}\n'
         '            limits: {cpu: "3", memory: 11Gi, nvidia.com/gpu: 1}\n'),
        ('    requests.cpu: "5"\n', '    requests.cpu: "5"\n    requests.nvidia.com/gpu: "1"\n'),
    )
    for old, new in edits:
        if old not in template:
            raise ValueError(f"GPU edit no longer applies: {old[:40]!r}")
        template = template.replace(old, new)
    bench = '      tolerations:\n        - {key: ewallet.io/bench, operator: Equal, value: "true", effect: NoSchedule}\n'
    if bench not in template:
        raise ValueError("bench toleration is missing")
    return template.replace(bench, GPU_TOLERATIONS)


def main(argv: list[str] | None = None) -> int:
    """Print the rendered stack for one experiment arm."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=sorted(ARMS))
    parser.add_argument("arm", choices=("baseline", "optimized"))
    parser.add_argument("--vllm-image", required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--api-server-cidr", required=True)
    parser.add_argument("--api-server-port", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    values = {**ARMS[args.experiment][args.arm], "VLLM_IMAGE": args.vllm_image, "GATEWAY_IMAGE": args.gateway_image,
              "API_SERVER_CIDR": args.api_server_cidr, "API_SERVER_PORT": args.api_server_port, "RUN_ID": args.run_id}
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
        if args.experiment == "exp-v3a":
            template = to_gpu(template, values.pop("MODEL_PATH"))
        sys.stdout.write(render(template, values))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
