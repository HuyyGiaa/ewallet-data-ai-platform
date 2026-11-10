#!/usr/bin/env python3
"""Render the benchmark stack for one arm of EXP-V1 or EXP-V2.

The arm table below is the whole difference between the arms. Everything else comes from
one template, so an arm cannot drift by accident. Images must be pinned by digest.

``--stack observability`` renders the isolated Prometheus and Grafana, and ``--stack
watchdog`` the in-cluster deadline watchdog. Neither depends on the arm, so both arms
are observed and guarded by identical objects. ``--stack drill`` renders only the
namespace, quota and network policies plus one idle pod, for the watchdog drill.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra/kubernetes/experiments/vllm-bench.yaml.tmpl"
OBSERVABILITY_TEMPLATE = ROOT / "infra/kubernetes/experiments/bench-observability.yaml.tmpl"
WATCHDOG_TEMPLATE = ROOT / "infra/kubernetes/experiments/bench-watchdog.yaml.tmpl"
# The images production observability already pins (infra/helm/llm-agent-platform/values.yaml).
PROMETHEUS_IMAGE = ("docker.io/prom/prometheus:v3.7.3@sha256:"
                    "6573196e5dbe834779ac4d34cb053113cc502a69e9e20ba3be96d37f33e81545")
GRAFANA_IMAGE = ("docker.io/grafana/grafana:12.3.0@sha256:"
                 "70d9599b186ce287be0d2c5ba9a78acb2e86c1a68c9c41449454d0fc3eeb84e8")
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

    images = {key for key in re.findall(r"__([A-Z_]+_IMAGE)__", template)} | {k for k in values if k.endswith("_IMAGE")}
    for key in sorted(images):
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


def block(text: str, spaces: int) -> str:
    """Indent a file for a YAML block scalar whose placeholder already sits at ``spaces``."""

    return ("\n" + " " * spaces).join(text.rstrip("\n").splitlines())


def render_observability(experiment: str, values: Mapping[str, str]) -> str:
    """The isolated Prometheus and Grafana, with the exporter code and dashboard inlined."""

    sys.path.insert(0, str(ROOT))
    from ops.experiments.bench_dashboard import dashboard_json

    template = OBSERVABILITY_TEMPLATE.read_text(encoding="utf-8")
    code = (ROOT / "ops/experiments/pod_usage_exporter.py").read_text(encoding="utf-8")
    return render(template, {**values, "EXPERIMENT": experiment, "PROMETHEUS_IMAGE": PROMETHEUS_IMAGE,
                             "GRAFANA_IMAGE": GRAFANA_IMAGE, "EXPORTER_CODE": block(code, 4),
                             "DASHBOARD_JSON": block(dashboard_json(), 4)})


def render_watchdog(values: Mapping[str, str]) -> str:
    """The deadline watchdog; the deadline must be an explicit UTC timestamp."""

    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", values.get("DEADLINE_UTC", "")):
        raise ValueError("--deadline-utc must look like 2026-11-09T04:00:00Z")
    code = (ROOT / "ops/experiments/bench_watchdog.py").read_text(encoding="utf-8")
    return render(WATCHDOG_TEMPLATE.read_text(encoding="utf-8"), {**values, "WATCHDOG_CODE": block(code, 4)})


DRILL_POD = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: drill-idle
  namespace: ewallet-bench
  labels: {app.kubernetes.io/name: drill-idle}
spec:
  replicas: 1
  selector:
    matchLabels: {app.kubernetes.io/name: drill-idle}
  template:
    metadata:
      labels: {app.kubernetes.io/name: drill-idle}
    spec:
      automountServiceAccountToken: false
      enableServiceLinks: false
      nodeSelector: {ewallet.io/bench: "true"}
      tolerations:
        - {key: ewallet.io/bench, operator: Equal, value: "true", effect: NoSchedule}
      securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: idle
          image: __HELPER_IMAGE__
          command: [python3, -c, "import time; time.sleep(86400)"]
          resources:
            requests: {cpu: 10m, memory: 32Mi}
            limits: {cpu: 50m, memory: 64Mi}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          # Local storage, like vLLM: by itself it would keep the autoscaler from removing the node.
          volumeMounts:
            - {name: scratch, mountPath: /scratch}
      volumes:
        - name: scratch
          emptyDir: {sizeLimit: 16Mi}
"""


def render_drill(serving: str, helper_image: str) -> str:
    """Namespace, quota and policies from the serving stack, plus one idle pod with local storage."""

    keep = [doc for doc in serving.split("\n---\n")
            if re.search(r"^kind: (Namespace|ResourceQuota|NetworkPolicy)$", doc, re.MULTILINE)]
    return "\n---\n".join([*keep, render(DRILL_POD, {"HELPER_IMAGE": helper_image})])


def api_peers(cidrs: str) -> str:
    """NetworkPolicy peers for the API server: Service ClusterIP and endpoint, comma separated.

    In-cluster clients dial the ClusterIP; listing the endpoint too keeps the policy correct
    whether the CNI evaluates egress before or after the Service translation.
    """

    items = [item.strip() for item in cidrs.split(",") if item.strip()]
    if not items or not all(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}/32", item) for item in items):
        raise ValueError("--api-server-cidr must be one or more comma-separated /32 addresses")
    return "[" + ", ".join(f"{{ipBlock: {{cidr: {item}}}}}" for item in items) + "]"


def dns_peers(addresses: str) -> str:
    """Extra DNS egress peers (NodeLocal DNSCache and kube-dns ClusterIP), one list item each."""

    items = [item.strip() for item in addresses.split(",") if item.strip()]
    if not items or not all(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", item) for item in items):
        raise ValueError("--dns-ips must be one or more comma-separated IPv4 addresses")
    return "\n        ".join(f"- {{ipBlock: {{cidr: {item}/32}}}}" for item in items)


def main(argv: list[str] | None = None) -> int:
    """Print the rendered stack for one experiment arm, or the observability or watchdog stack."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=sorted(ARMS))
    parser.add_argument("arm", choices=("baseline", "optimized"))
    parser.add_argument("--stack", choices=("serving", "observability", "watchdog", "drill"), default="serving")
    parser.add_argument("--vllm-image", default="")
    parser.add_argument("--gateway-image", default="")
    parser.add_argument("--helper-image", default="", help="python3 image for the exporter and the watchdog")
    parser.add_argument("--deadline-utc", default="")
    parser.add_argument("--api-server-cidr", required=True, help="comma-separated /32s: endpoint and ClusterIP")
    parser.add_argument("--api-server-port", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dns-ips", default="169.254.20.10",
                        help="NodeLocal DNSCache address and kube-dns ClusterIP, comma separated")
    args = parser.parse_args(argv)
    common = {"API_SERVER_PEERS": api_peers(args.api_server_cidr), "API_SERVER_PORT": args.api_server_port}
    try:
        if args.stack == "observability":
            sys.stdout.write(render_observability(args.experiment, {**common, "HELPER_IMAGE": args.helper_image}))
            return 0
        if args.stack == "watchdog":
            sys.stdout.write(render_watchdog({**common, "HELPER_IMAGE": args.helper_image,
                                              "DEADLINE_UTC": args.deadline_utc}))
            return 0
        if not args.run_id:
            raise ValueError("--run-id is required for the serving stack")
        values = {**ARMS[args.experiment][args.arm], "VLLM_IMAGE": args.vllm_image,
                  "GATEWAY_IMAGE": args.gateway_image, **common, "RUN_ID": args.run_id,
                  "DNS_PEERS": dns_peers(args.dns_ips)}
        template = TEMPLATE.read_text(encoding="utf-8")
        if args.experiment == "exp-v3a":
            template = to_gpu(template, values.pop("MODEL_PATH"))
        text = render(template, values)
        sys.stdout.write(render_drill(text, args.helper_image) if args.stack == "drill" else text)
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
