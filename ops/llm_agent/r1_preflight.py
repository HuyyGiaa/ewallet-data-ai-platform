"""R1 runtime preflight: GO/NO-GO before starting CPU vLLM behind agentgateway.

Read-only. It never changes the kubeconfig, never installs anything and never
reads Secret data. Checks:

- context: an explicit ``--kube-context`` from the allow-list; any ``gke_``
  context is refused, the kubeconfig default is never used;
- cluster: API reachable, kagent ModelConfig CRD served, namespace, the vLLM
  API-key Secret (existence only) and the staged model claim exist;
- chart: values-local renders (timeout-nesting and retry guards pass); the vLLM
  and agentgateway images are digest-pinned; the staged model path carries the
  pinned revision; tool calling uses --enable-auto-tool-choice with the
  hermes parser; qwen3 reasoning parser with thinking disabled; resource
  limits set; exactly one primary and no fallback vLLM pod; incomplete
  components (llm-d, agents, gateways) stay disabled;
- kind node: the node container is capped (Docker cgroup) and the vLLM
  limits fit inside it;
- host: DP2 terminal (no Dataproc batch submit or DP2 task process), Airflow
  8084 healthy, MemAvailable leaves the 8 GiB floor after the vLLM memory
  limit, normalized load low.

usage:
  python3 -m ops.llm_agent.r1_preflight --kube-context kind-llm-agent-local \
      [--values values-r1-local.yaml ...] [--json-output report.json]
Without --values, values-local.yaml + values-r1-local.yaml are rendered.
exit 0 = GO, 1 = NO-GO, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
ALLOWED_CONTEXTS = ("kind-llm-agent-local",)
NAMESPACE = "ewallet-agents"
MEM_FLOOR_GIB = 8.0
KIND_NODE = "llm-agent-local-control-plane"
NODE_CPU_CAP = 4.0
NODE_MEM_CAP_GIB = 9.0
MAX_NORMALIZED_LOAD = 0.5
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
DP2_PROCESS_RE = re.compile(r"dataproc batches submit|airflow tasks run .*dp2|dp2_method2")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=False, timeout=60)


def check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "status": "PASS" if ok else "FAIL", "detail": detail}


def context_checks(context: str | None) -> list[dict[str, Any]]:
    if not context:
        return [check("explicit-context", False, "--kube-context is required; the kubeconfig default is never used")]
    if context.startswith("gke_"):
        return [check("explicit-context", False, f"{context} is a GKE context; R1 runs only on the local kind cluster")]
    return [check("explicit-context", context in ALLOWED_CONTEXTS,
                  f"{context} {'is' if context in ALLOWED_CONTEXTS else 'is not'} in {list(ALLOWED_CONTEXTS)}")]


def cluster_checks(context: str, run: Runner = _run, *, claim: str = "vllm-model-staged") -> list[dict[str, Any]]:
    kubectl = ["kubectl", "--context", context]
    results = []
    version = run([*kubectl, "version", "-o", "json"])
    results.append(check("api-reachable", version.returncode == 0, "kubectl version" if version.returncode == 0
                         else version.stderr.strip()[:200]))
    crd = run([*kubectl, "get", "crd", "modelconfigs.kagent.dev", "-o", "name"])
    results.append(check("modelconfig-crd", crd.returncode == 0, "modelconfigs.kagent.dev"))
    namespace = run([*kubectl, "get", "namespace", NAMESPACE, "-o", "name"])
    results.append(check("namespace", namespace.returncode == 0, NAMESPACE))
    # Existence only: never -o yaml/json on a Secret.
    secret = run([*kubectl, "-n", NAMESPACE, "get", "secret", "vllm-api-key", "-o", "name"])
    results.append(check("vllm-api-key-secret", secret.returncode == 0, "secret/vllm-api-key exists (data not read)"))
    pvc = run([*kubectl, "-n", NAMESPACE, "get", "pvc", claim, "-o", "jsonpath={.status.phase}"])
    results.append(check("staged-model-claim", pvc.returncode == 0 and pvc.stdout.strip() == "Bound",
                         f"pvc/{claim} phase={pvc.stdout.strip() or 'missing'}"))
    return results


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    return next((d for d in documents if d.get("kind") == kind and d["metadata"]["name"] == name), None)


def chart_checks(documents: list[dict[str, Any]], values: Mapping[str, Any]) -> list[dict[str, Any]]:
    results = []
    primary = _find(documents, "Deployment", "vllm-primary")
    fallback = _find(documents, "Deployment", "vllm-fallback")
    gateway = _find(documents, "Deployment", "agentgateway")
    if not (primary and fallback and gateway):
        return [check("rendered-workloads", False, "vllm-primary, vllm-fallback and agentgateway must render")]
    container = primary["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    flag = lambda name: args[args.index(name) + 1] if name in args else None  # noqa: E731
    vllm = values["vllm"]
    results.append(check("vllm-image-pinned", bool(DIGEST_RE.search(container["image"])), container["image"]))
    gw_image = gateway["spec"]["template"]["spec"]["containers"][0]["image"]
    results.append(check("agentgateway-image-pinned", bool(DIGEST_RE.search(gw_image)), gw_image))
    model = flag("--model") or ""
    results.append(check("model-revision", model.endswith(vllm["modelRevision"]) and "--revision" not in args,
                         f"--model {model}"))
    results.append(check("served-model-name", flag("--served-model-name") == vllm["servedModelName"],
                         f"--served-model-name {flag('--served-model-name')}"))
    results.append(check("tool-calling", "--enable-auto-tool-choice" in args and flag("--tool-call-parser") == "hermes",
                         f"--tool-call-parser {flag('--tool-call-parser')}"))
    kwargs = json.loads(flag("--default-chat-template-kwargs") or "{}")
    results.append(check("reasoning", flag("--reasoning-parser") == "qwen3" and kwargs.get("enable_thinking") is False,
                         f"--reasoning-parser {flag('--reasoning-parser')} kwargs={kwargs}"))
    limits = container.get("resources", {}).get("limits", {})
    results.append(check("vllm-limits", {"cpu", "memory"} <= set(limits), f"limits={limits}"))
    replicas = (primary["spec"]["replicas"], fallback["spec"]["replicas"])
    results.append(check("single-vllm-pod", replicas == (1, 0), f"primary/fallback replicas={replicas}"))
    config = yaml.safe_load(_find(documents, "ConfigMap", "agentgateway-llm-config")["data"]["config.yaml"])
    served = {m["params"]["model"] for m in config["llm"]["models"]}
    model_config = _find(documents, "ModelConfig", values["modelConfig"]["name"])
    virtual = {v["name"] for v in config["llm"]["virtualModels"]}
    results.append(check("model-identity",
                         served == {vllm["servedModelName"]} and model_config is not None
                         and model_config["spec"]["model"] in virtual
                         and model_config["spec"]["openAI"]["baseUrl"].startswith("http://agentgateway."),
                         f"ModelConfig model={model_config and model_config['spec']['model']} virtual={sorted(virtual)} "
                         f"served={sorted(served)}"))
    entry = config["routes"][0]["policies"]
    results.append(check("entry-no-per-hop-timeout", "backendRequestTimeout" not in entry["timeout"]
                         and not set(entry.get("retry", {}).get("codes", [])) & {500, 504},
                         f"entry policies={entry}"))
    enabled = sorted(k for k in ("llmd", "agents", "mcpGateway", "a2aGateway", "mcpServices")
                     if values.get(k, {}).get("enabled"))
    results.append(check("incomplete-components-disabled", not enabled, f"enabled={enabled or 'none'}"))
    namespace = _find(documents, "Namespace", values["namespace"]["name"])
    ambient = bool(namespace) and namespace["metadata"].get("labels", {}).get("istio.io/dataplane-mode") == "ambient"
    results.append(check("namespace-not-ambient-for-r1", not ambient,
                         "R1 keeps the namespace out of ambient (identity is a separate gate)"))
    return results


def _mem_gib(quantity: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Gi|Mi)", quantity)
    if not match:
        raise ValueError(f"unsupported memory quantity {quantity!r}")
    value = float(match.group(1))
    return value if match.group(2) == "Gi" else value / 1024


def host_checks(values: Mapping[str, Any], *, meminfo: str, loadavg: str, cpus: int, processes: str,
                airflow_health: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    results = []
    dp2 = [line.strip()[:120] for line in processes.splitlines() if DP2_PROCESS_RE.search(line)]
    results.append(check("dp2-terminal", not dp2, "no DP2 process" if not dp2 else f"active: {dp2[:2]}"))
    healthy = bool(airflow_health) and all(
        (airflow_health.get(part) or {}).get("status") == "healthy" for part in ("metadatabase", "scheduler"))
    results.append(check("airflow-healthy", healthy, "airflow 8084 metadatabase+scheduler healthy" if healthy
                         else f"airflow health={airflow_health}"))
    available_kib = int(re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, re.M).group(1))
    available = available_kib / 1024 / 1024
    limit = _mem_gib(values["vllm"]["resources"]["limits"]["memory"])
    results.append(check("memory-headroom", available - limit >= MEM_FLOOR_GIB,
                         f"MemAvailable {available:.1f} GiB - vLLM limit {limit:.1f} GiB "
                         f"must stay >= {MEM_FLOOR_GIB} GiB"))
    load = float(loadavg.split()[0]) / cpus
    results.append(check("cpu-load", load <= MAX_NORMALIZED_LOAD, f"normalized load {load:.2f} (max {MAX_NORMALIZED_LOAD})"))
    return results


def _cpu(quantity: str) -> float:
    return float(quantity[:-1]) / 1000 if quantity.endswith("m") else float(quantity)


def node_cap_checks(values: Mapping[str, Any], inspect: str) -> list[dict[str, Any]]:
    """``inspect``: output of docker inspect --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}'."""

    try:
        nano, memory = (int(part) for part in inspect.split())
    except ValueError:
        return [check("kind-node-capped", False, f"cannot read the {KIND_NODE} container limits")]
    cpus, mem_gib = nano / 1e9, memory / 1024 ** 3
    capped = 0 < cpus <= NODE_CPU_CAP and 0 < mem_gib <= NODE_MEM_CAP_GIB
    limits = values["vllm"]["resources"]["limits"]
    fits = _cpu(str(limits["cpu"])) <= cpus and _mem_gib(limits["memory"]) <= mem_gib
    return [check("kind-node-capped", capped, f"node container cpus={cpus:g} memory={mem_gib:.1f} GiB "
                  f"(cap {NODE_CPU_CAP:g} CPU / {NODE_MEM_CAP_GIB:g} GiB protects DP2)"),
            check("vllm-fits-node", fits, f"vLLM limits {limits} within the node cap")]


def render(values_files: Sequence[Path], run: Runner = _run) -> tuple[list[dict[str, Any]], str]:
    args = ["helm", "template", "llm-agent", str(CHART), "-f", str(CHART / "values-local.yaml")]
    for path in values_files:
        args += ["-f", str(path)]
    proc = run(args)
    if proc.returncode != 0:
        return [], proc.stderr.strip()[-300:]
    return [d for d in yaml.safe_load_all(proc.stdout) if d], ""


def merged_values(values_files: Sequence[Path]) -> dict[str, Any]:
    def merge(base: dict, override: dict) -> dict:
        out = dict(base)
        for key, value in override.items():
            out[key] = merge(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
        return out
    values: dict[str, Any] = {}
    for path in (CHART / "values.yaml", CHART / "values-local.yaml", *values_files):
        values = merge(values, yaml.safe_load(Path(path).read_text()) or {})
    return values


def _airflow_health() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen("http://localhost:8084/api/v2/monitor/health", timeout=5) as response:  # noqa: S310
            return json.load(response)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kube-context")
    parser.add_argument("--values", type=Path, action="append", default=[])
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    results = context_checks(args.kube_context)
    if results[0]["status"] == "PASS":
        results += cluster_checks(args.kube_context)
    values_files = args.values or [CHART / "values-r1-local.yaml"]
    documents, error = render(values_files)
    values = merged_values(values_files)
    results += node_cap_checks(values, _run(["docker", "inspect", KIND_NODE, "--format",
                                             "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}"]).stdout)
    results += chart_checks(documents, values) if documents else [check("chart-render", False, error)]
    results += host_checks(values, meminfo=Path("/proc/meminfo").read_text(), loadavg=Path("/proc/loadavg").read_text(),
                           cpus=os.cpu_count() or 1, processes=_run(["ps", "-eo", "args"]).stdout,
                           airflow_health=_airflow_health())
    verdict = "GO" if all(item["status"] == "PASS" for item in results) else "NO-GO"
    report = {"verdict": verdict, "kube_context": args.kube_context, "checks": results}
    text = json.dumps(report, indent=1)
    if args.json_output:
        args.json_output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
