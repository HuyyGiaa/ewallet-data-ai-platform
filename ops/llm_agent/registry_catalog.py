"""Publish the E-Wallet agents, prompts and MCP servers to agentregistry.

The catalog is derived from the same sources the chart deploys
(infra/helm/llm-agent-platform values.yaml ``agents`` and prompts/), so the
registry entry, the SandboxAgent and the prompt ConfigMap cannot drift:

- Prompt ``<agent>-system`` tag ``<promptVersion>``: the exact prompt text,
  sha256 in labels.
- MCPServer ``<server>``: remote Streamable HTTP endpoint behind the MCP Gateway.
- Agent ``<agent>`` tag ``<version>``: title, description, A2A protocol,
  instructions -> its Prompt tag, MCP server refs, rubric rows and git SHA.

usage:
  python -m ops.llm_agent.registry_catalog render [--git-sha SHA] > catalog.yaml
  python -m ops.llm_agent.registry_catalog render --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml
  python -m ops.llm_agent.registry_catalog publish --registry http://127.0.0.1:12121 \
      --confirm-live-write http://127.0.0.1:12121 [--evidence out.json]
  python -m ops.llm_agent.registry_catalog publish --registry URL --dry-run

A live publish needs an explicit ``--registry`` and ``--confirm-live-write`` equal to
it: a port-forward to the GKE registry and a local registry share 127.0.0.1:12121,
so no write may rely on a default URL (incident F9, 2026-11-03).

``publish`` posts the multi-document YAML to ``/v0/apply`` and then reads every
entry back (``/v0/{agents,prompts,mcpservers}/{name}/{tag}``), comparing the
prompt sha256 and tags. An optional bearer token comes from ARCTL_API_TOKEN and
is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
import urllib.error
import urllib.request

import yaml

from ops.llm_agent.prompt_registry import gate as prompt_gate

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
API_VERSION = "ar.dev/v1alpha1"
KIND_PATHS = {"Agent": "agents", "Prompt": "prompts", "MCPServer": "mcpservers"}
BYO_MCP_SERVERS = {"feature-rag-specialist": ("feature-rag-mcp",), "drift-specialist": ("drift-mcp",)}


def merge_values(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Helm-style deep merge: maps merge, everything else (lists included) is replaced."""

    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = merge_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def _agents(values: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    agents = dict(values["specialists"])
    agents[values["coordinator"]["name"]] = values["coordinator"]
    return agents


def _prompt_document(name: str, version: str, display_name: str, entry: Mapping[str, Any],
                     prompts_dir: Path) -> dict[str, Any]:
    text = (prompts_dir / entry["path"]).read_text(encoding="utf-8")
    if hashlib.sha256(text.encode()).hexdigest() != entry["sha256"]:
        raise ValueError(f"prompt {name}/{version} does not match its manifest sha256")
    return {
        "apiVersion": API_VERSION, "kind": "Prompt",
        "metadata": {"name": f"{name}-system", "tag": version,
                     "labels": {"ewallet.io/agent": name, "ewallet.io/prompt-sha256": entry["sha256"][:63]}},
        "spec": {"description": f"System prompt of {display_name} ({entry['sha256']})", "content": text},
    }


def sandbox_specialists(values: Mapping[str, Any]) -> set[str]:
    """Specialists the Coordinator delegates to as SandboxAgents (PKG-P4: coordinator.byo.sandboxSpecialists)."""

    raw = ((values.get("coordinator") or {}).get("byo") or {}).get("sandboxSpecialists") or []
    if isinstance(raw, str):
        raw = raw.strip("{}").split(",")
    return {str(item).strip() for item in raw if str(item).strip()}


def agent_runtime(values: Mapping[str, Any], spec: Mapping[str, Any], model_config: str,
                  name: str = "") -> tuple[str, str]:
    """The runtime the chart deploys for one agent (templates/agents.yaml), as label and text.

    BYO wins over the agent mode (the chart renders ``type: BYO``); otherwise
    ``agents.mode`` selects a Substrate ``SandboxAgent`` or a kagent ``Agent``. A BYO
    Specialist that the Coordinator delegates to through the A2A Gateway sandbox route
    (PKG-P4) runs as a SandboxAgent with the same image, and its Deployment stays as the
    fallback. The text states configuration only; execution is verified by the E2E runs.
    """

    if (spec.get("byo") or {}).get("enabled"):
        if name and name in sandbox_specialists(values):
            return ("kagent-sandboxagent-byo",
                    "BYO image served as a kagent 0.10.3 SandboxAgent on the Substrate WorkerPool business-agents "
                    "(gVisor) behind the A2A Gateway sandbox route; the BYO Deployment is kept as the fallback")
        return "kagent-byo", "kagent 0.10.3 BYO Agent (own A2A server image)"
    if values.get("mode", "sandbox") == "sandbox":
        return ("kagent-sandboxagent",
                f"kagent 0.10.3 Declarative SandboxAgent (agents.mode=sandbox, ModelConfig {model_config})")
    return "kagent-declarative", f"kagent 0.10.3 Declarative Agent (ModelConfig {model_config})"


def build_catalog(values: Mapping[str, Any], prompts_dir: Path, git_sha: str, *,
                  all_prompt_versions: bool = False, model_config: str = "ewallet-global-llm",
                  sandbox_image: str = "") -> list[dict[str, Any]]:
    """Catalog documents; ``all_prompt_versions`` also publishes every pinned older/newer prompt."""

    manifest = json.loads((prompts_dir / "manifest.json").read_text(encoding="utf-8"))["prompts"]
    documents: list[dict[str, Any]] = []
    agents = _agents(values)
    for name in sorted(agents):
        spec = agents[name]
        versions = sorted(key.split("/", 1)[1] for key in manifest if key.split("/", 1)[0] == name) \
            if all_prompt_versions else [spec["promptVersion"]]
        for version in versions:
            documents.append(_prompt_document(name, version, spec["displayName"], manifest[f"{name}/{version}"],
                                              prompts_dir))
    for server in sorted(values["mcpServers"]):
        route = values["mcpServers"][server]
        documents.append({
            "apiVersion": API_VERSION, "kind": "MCPServer",
            "metadata": {"name": server, "tag": "v1", "labels": {"ewallet.io/tools": ".".join(route["tools"])}},
            "spec": {"title": server, "description": f"{server} via the policy-only MCP Gateway; tools: {', '.join(route['tools'])}",
                     "remote": {"type": "streamable-http", "url": values["mcpGatewayUrl"] + route["path"]}},
        })
    for name in sorted(agents):
        spec = agents[name]
        servers = sorted(spec.get("tools", {}))
        byo = spec.get("byo") or {}
        if byo.get("enabled"):
            # A BYO runtime calls only the MCP route its module is bound to (byo_*.py).
            servers = [server for server in servers if server in BYO_MCP_SERVERS.get(name, ())]
        runtime, runtime_text = agent_runtime(values, spec, model_config, name)
        labels = {"ewallet.io/rubric-rows": spec["rubricRows"].replace(",", "."),
                  "ewallet.io/git-sha": git_sha[:40],
                  "ewallet.io/runtime": runtime}
        digest = re.search(r"@sha256:([0-9a-f]{64})$", str(byo.get("image") or ""))
        if digest:
            # The BYO Deployment image (a label value stays short; the readback compares it).
            labels["ewallet.io/byo-image-digest"] = "sha256-" + digest.group(1)[:16]
        sandbox_digest = re.search(r"@sha256:([0-9a-f]{64})$", sandbox_image)
        if runtime == "kagent-sandboxagent-byo" and sandbox_digest:
            # The SandboxAgent fleet image (one image for both Specialists, not Helm-managed).
            labels["ewallet.io/sandbox-image-digest"] = "sha256-" + sandbox_digest.group(1)[:16]
        if "drift" in (byo.get("workflows") or []):
            labels["ewallet.io/workflows"] = ".".join(byo["workflows"])
        documents.append({
            "apiVersion": API_VERSION, "kind": "Agent",
            "metadata": {"name": name, "tag": spec["version"], "labels": labels},
            "spec": {
                "title": spec["displayName"],
                "description": f"{spec['displayName']}: {runtime_text}, deployed by llm-agent-platform",
                "source": {"protocol": "A2A"},
                # agentregistry 0.4.0 rejects instructions without a harness
                # (found by the local registry smoke); kagent runs these agents.
                "compatibleHarnesses": [{"type": "kagent"}],
                "instructions": {"kind": "Prompt", "name": f"{name}-system", "tag": spec["promptVersion"]},
                "mcpServers": [{"kind": "MCPServer", "name": server, "tag": "v1"} for server in servers],
            },
        })
    return documents


def schema_errors(value: Any, schema: Mapping[str, Any], schemas: Mapping[str, Any], path: str = "") -> list[str]:
    """Check documents against the vendored agentregistry OpenAPI subset."""

    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name not in schemas:
            return []
        schema = schemas[name]
    errors: list[str] = []
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if "object" in kinds and isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing {name}")
        properties = schema.get("properties", {})
        for name, child in value.items():
            if name in properties:
                errors.extend(schema_errors(child, properties[name], schemas, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown field {name}")
    elif "array" in kinds and isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema.get("items", {}), schemas, f"{path}[{index}]"))
    elif "string" in kinds and not isinstance(value, str):
        errors.append(f"{path}: expected string")
    return errors


def _local_only(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("registry must be a local port-forward: http://127.0.0.1:<port>")
    return url.rstrip("/")


Opener = Callable[[urllib.request.Request], tuple[int, bytes]]


def _open(request: urllib.request.Request) -> tuple[int, bytes]:
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (local-only URL checked)
        return response.status, response.read()


def publish(documents: list[dict[str, Any]], registry: str, *, dry_run: bool, opener: Opener = _open,
            token: str | None = None) -> dict[str, Any]:
    base = _local_only(registry)
    headers = {"content-type": "application/yaml"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = yaml.safe_dump_all(documents, sort_keys=False).encode()
    status, raw = opener(urllib.request.Request(
        f"{base}/v0/apply?dryRun={'true' if dry_run else 'false'}", body, headers, method="POST"))
    results = json.loads(raw or b"{}").get("results") or []
    report: dict[str, Any] = {"apply_status": status, "dry_run": dry_run, "apply": results, "readback": []}
    failed = [r for r in results if r.get("error")]
    if status >= 300 or failed or len(results) != len(documents):
        report["verdict"] = "FAIL"
        return report
    if not dry_run:
        for document in documents:
            name, tag = document["metadata"]["name"], document["metadata"]["tag"]
            path = f"{base}/v0/{KIND_PATHS[document['kind']]}/{name}/{tag}"
            read_status, read_raw = opener(urllib.request.Request(path, headers={k: v for k, v in headers.items()
                                                                              if k == "authorization"}))
            stored = json.loads(read_raw or b"{}")
            stored = stored.get("item", stored) if isinstance(stored, dict) else stored
            match = read_status == 200 and stored.get("metadata", {}).get("tag") == tag
            if document["kind"] == "Prompt":
                match = match and stored.get("spec", {}).get("content") == document["spec"]["content"]
            report["readback"].append({"kind": document["kind"], "name": name, "tag": tag,
                                       "status": read_status, "match": match})
    report["verdict"] = "PASS" if all(item["match"] for item in report["readback"]) else "FAIL"
    return report


# Labels that record when an entry was published, not what it describes: a newer commit than the
# publication is not drift by itself, so readback reports them without failing.
PUBLICATION_LABELS = frozenset({"ewallet.io/git-sha"})


def _open_status(request: urllib.request.Request) -> tuple[int, bytes]:
    """Like _open, but an HTTP error status (404: not registered) is a result, not an exception."""

    try:
        return _open(request)
    except urllib.error.HTTPError as error:
        return error.code, error.read() or b""


def readback(documents: list[dict[str, Any]], registry: str, *, opener: Opener = _open_status) -> dict[str, Any]:
    """Read every catalog entry from the registry (GET only) and compare it with the catalog.

    ``registered`` means the registry holds the entry at this tag; ``matches`` means its spec and
    labels equal what the chart deploys (publication labels excepted). Neither says the runtime is
    deployed or reachable: AGI-13 checks that separately against the cluster.
    """

    base = _local_only(registry)
    entries = []
    for document in documents:
        name, tag = document["metadata"]["name"], document["metadata"]["tag"]
        status, raw = opener(urllib.request.Request(f"{base}/v0/{KIND_PATHS[document['kind']]}/{name}/{tag}"))
        try:
            stored = json.loads(raw or b"{}")
        except ValueError:
            stored = {}
        stored = stored.get("item", stored) if isinstance(stored, dict) else {}
        differences = []
        if status == 200:
            want_labels = document["metadata"].get("labels") or {}
            have_labels = (stored.get("metadata") or {}).get("labels") or {}
            for key in sorted(set(want_labels) | set(have_labels)):
                if key not in PUBLICATION_LABELS and want_labels.get(key) != have_labels.get(key):
                    differences.append(f"label {key}: registry {have_labels.get(key)!r}, catalog {want_labels.get(key)!r}")
            want_spec, have_spec = document.get("spec") or {}, stored.get("spec") or {}
            for key in sorted(want_spec):
                if want_spec[key] != have_spec.get(key):
                    differences.append(f"spec.{key} differs")
        entries.append({"kind": document["kind"], "name": name, "tag": tag, "status": status,
                        "registered": status == 200, "matches": status == 200 and not differences,
                        "published_git_sha": ((stored.get("metadata") or {}).get("labels") or {}).get("ewallet.io/git-sha"),
                        "differences": differences})
    verdict = "PASS" if entries and all(item["matches"] for item in entries) else "FAIL"
    return {"verdict": verdict, "registry": base, "entries": entries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["render", "publish", "readback"])
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--registry")
    parser.add_argument("--restore-from-git", action="store_true",
                        help="restore after tampering: the pre gate tolerates MISMATCH; the post gate stays strict")
    parser.add_argument("--confirm-live-write", metavar="URL",
                        help="required for a non-dry-run publish; must equal --registry")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--all-prompt-versions", action="store_true",
                        help="publish every version pinned in prompts/manifest.json, not only the deployed one")
    parser.add_argument("--sandbox-image", default="",
                        help="digest-pinned image of the SandboxAgent fleet (ewallet-sandbox), for its label")
    parser.add_argument("--values-overlay", type=Path, action="append", default=[],
                        help="chart values file layered on values.yaml, as Helm would (e.g. agi/agents.yaml)")
    args = parser.parse_args(argv)
    git_sha = args.git_sha or subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                             text=True, check=True).stdout.strip()
    chart_values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    for overlay in args.values_overlay:
        chart_values = merge_values(chart_values, yaml.safe_load(overlay.read_text(encoding="utf-8")) or {})
    values = chart_values["agents"]
    documents = build_catalog(values, CHART / "prompts", git_sha, all_prompt_versions=args.all_prompt_versions,
                              model_config=chart_values["modelConfig"]["name"], sandbox_image=args.sandbox_image)
    if args.command == "render":
        yaml.safe_dump_all(documents, sys.stdout, sort_keys=False)
        return 0
    if not args.registry:
        parser.error(f"{args.command} requires an explicit --registry")
    if args.command == "readback":
        report = readback(documents, args.registry)
        report["git_sha"] = git_sha
        text = json.dumps(report, indent=1, sort_keys=True)
        if args.evidence:
            args.evidence.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if report["verdict"] == "PASS" else 1
    if not args.dry_run and args.confirm_live_write != args.registry:
        parser.error("a live publish requires --confirm-live-write equal to --registry")
    gates: dict[str, Any] = {}
    if not args.dry_run:
        ok, gates["pre"] = prompt_gate(args.registry, "pre", allow_tampered=args.restore_from_git)
        if not ok:
            print(json.dumps({"verdict": "FAIL", "reason": "M3 pre-publication gate", "gate": gates},
                             indent=1, sort_keys=True))
            return 1
    report = publish(documents, args.registry, dry_run=args.dry_run, token=os.environ.get("ARCTL_API_TOKEN"))
    if not args.dry_run:
        ok, gates["post"] = prompt_gate(args.registry, "post")
        report["gate"] = gates
        if not ok:
            report["verdict"] = "FAIL"
    report["git_sha"] = git_sha
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.evidence:
        args.evidence.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
