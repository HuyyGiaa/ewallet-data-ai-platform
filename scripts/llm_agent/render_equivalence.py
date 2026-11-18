#!/usr/bin/env python3
"""Offline Helm render equivalence for the llm-agent-platform chart (CI/CD V2-P1).

Renders a fixed set of source-defined production profiles with ``helm template`` (no cluster access, no
``--validate``, no lookup), writes each raw manifest and an inventory, and compares two such snapshots
byte for byte. A snapshot can be taken from the working tree or from any Git ref (``git archive`` of the
chart directory into a temporary directory, so no worktree or checkout is needed).

The profiles reproduce the repository's own production renders (``tests/llm_agent/test_agi_chart.py``
inputs plus the ``agi/`` overlays the agi_campaign packages apply). The live release values are private
(``AGI_PRIVATE``) and are not part of this comparison: a byte-identical result proves the chart renders the
same output for the same source-defined inputs, not that the live release state is unchanged.

    python3 scripts/llm_agent/render_equivalence.py render --out DIR [--ref GIT_REF]
    python3 scripts/llm_agent/render_equivalence.py compare BASELINE_DIR CURRENT_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CHART_PATH = "infra/helm/llm-agent-platform"
DIGEST = "sha256:" + "a" * 64
BYO_IMAGE = "asia-southeast1-docker.pkg.dev/p/r/agent@sha256:" + "b" * 64

COMMON = ("--set", f"vllm.image=r.example/p/vllm@{DIGEST}", "--set", f"mcpServices.image=r.example/p/mcp@{DIGEST}",
          "--set", f"mcpServices.identity.imageDigest={DIGEST}", "--set", "mcpServices.identity.gitSha=" + "c" * 40)
AGENTS = ("-f", "values-gke-cpu.yaml", "-f", "values-gke-cpu-g1.yaml", "-f", "g3/g3.yaml",
          "-f", "n11/values-n11-agents.yaml", "--set", "idempotencyRedis.enabled=true",
          "--set", "otelCollector.enabled=true")
AGI_AGENTS = ("-f", "agi/agents.yaml", "--set", f"agents.coordinator.byo.image={BYO_IMAGE}",
              "--set", f"agents.specialists.feature-rag-specialist.byo.image={BYO_IMAGE}",
              "--set", f"agents.specialists.drift-specialist.byo.image={BYO_IMAGE}",
              "--set", "modelConfig.byoResolver.apiServer.cidrs={10.0.0.1/32}")
SANDBOX_ROUTES = ("--set", "a2aGateway.sandboxRoutes.namespace=ewallet-sandbox",
                  "--set", "a2aGateway.sandboxRoutes.agents={drift-specialist,feature-rag-specialist}",
                  "--set", "agents.coordinator.byo.sandboxSpecialists={drift-specialist,feature-rag-specialist}")
MCP = ("-n", "ewallet-mcp", "-f", "values-gke-cpu.yaml", "-f", "g3/g3.yaml", "-f", "g3/mcp-isolated.yaml")
AGI_MCP_SETS = ("--set", "mcpServices.identity.configRevision=agi-20261115T000000Z",
                "--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ONLINE_DB=0",
                "--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_RAG_ONLINE_DB=3",
                "--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ALLOW_PRODUCTION_DB=true",
                "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_BASELINES_SHA256=" + "d" * 64,
                "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_WINDOW_MANIFEST_SHA256=" + "e" * 64,
                "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_DATASET_REVISION=feast-r5gb-20261111-214544")

# name -> (release, args). Values order is significant and fixed here.
PRODUCTION_PROFILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "chart-defaults": ("llm-agent", ()),
    "values-gcp": ("llm-agent", ("-f", "values-gcp.yaml")),
    "values-dev": ("llm-agent", ("-f", "values-dev.yaml")),
    "values-local": ("llm-agent", ("-f", "values-local.yaml")),
    "gke-cpu-agents": ("llm-agent", AGENTS + ("--set", "agents.coordinator.byo.enabled=true",
                                             "--set", "agents.specialists.feature-rag-specialist.byo.enabled=true")),
    "agi-agents": ("llm-agent", AGENTS + AGI_AGENTS),
    # Closest source-defined profile to the live llm-agent release: AGI agents, Sandbox-only Specialists
    # (sp-cleanup) and the in-release drift-mcp retirement (lc-agents).
    "agi-agents-sandbox": ("llm-agent", AGENTS + AGI_AGENTS + SANDBOX_ROUTES
                           + ("-f", "agi/specialists-sandbox-only.yaml", "-f", "agi/agents-retire-in-release-mcp.yaml")),
    "agi-mcp": ("llm-agent-mcp", MCP + AGI_MCP_SETS),
}


def helm_binary() -> str:
    helm = shutil.which("helm")
    if not helm:
        raise SystemExit("helm binary unavailable")
    return helm


def helm_version(helm: str) -> str:
    return subprocess.run([helm, "version", "--short"], capture_output=True, text=True, check=True).stdout.strip()


def render(helm: str, chart: Path, release: str, args: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.run([helm, "template", release, str(chart), *args, *COMMON], cwd=chart,
                          capture_output=True, text=True)


def inventory(manifest: str) -> dict:
    import yaml  # only the inventory needs PyYAML; the byte comparison does not

    docs = [doc for doc in yaml.safe_load_all(manifest) if doc]
    identities = sorted(f"{d.get('apiVersion')}|{d.get('kind')}|{(d.get('metadata') or {}).get('namespace', '')}|"
                        f"{(d.get('metadata') or {}).get('name')}" for d in docs)
    namespaces: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for doc in docs:
        ns = (doc.get("metadata") or {}).get("namespace", "<none>")
        namespaces[ns] = namespaces.get(ns, 0) + 1
        kinds[doc["kind"]] = kinds.get(doc["kind"], 0) + 1
    return {"objects": len(docs), "namespaces": dict(sorted(namespaces.items())), "kinds": dict(sorted(kinds.items())),
            "identities": identities}


def chart_from_ref(ref: str, into: Path) -> Path:
    archive = subprocess.run(["git", "archive", ref, CHART_PATH], cwd=ROOT, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive, check=True)
    return into / CHART_PATH


def snapshot(out: Path, chart: Path, profiles: dict[str, tuple[str, tuple[str, ...]]], source: str) -> dict:
    helm = helm_binary()
    out.mkdir(parents=True, exist_ok=True)
    summary = {"helm": helm_version(helm), "source": source, "profiles": {}}
    for name, (release, args) in profiles.items():
        result = render(helm, chart, release, args)
        if result.returncode != 0:
            raise SystemExit(f"profile {name} failed to render: {result.stderr.strip()[-600:]}")
        (out / f"{name}.yaml").write_text(result.stdout)
        summary["profiles"][name] = {"release": release, "args": list(args) + list(COMMON),
                                     "sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                                     **inventory(result.stdout)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def compare(baseline: Path, current: Path) -> int:
    base = json.loads((baseline / "summary.json").read_text())
    cur = json.loads((current / "summary.json").read_text())
    failed = False
    if base["helm"] != cur["helm"]:
        print(f"HELM_VERSION_DIFFERS baseline={base['helm']} current={cur['helm']}")
        failed = True
    for name in sorted(set(base["profiles"]) | set(cur["profiles"])):
        b, c = base["profiles"].get(name), cur["profiles"].get(name)
        if not b or not c:
            print(f"{name}: PROFILE_MISSING baseline={bool(b)} current={bool(c)}")
            failed = True
            continue
        if b["args"] != c["args"] or b["release"] != c["release"]:
            print(f"{name}: INPUTS_DIFFER")
            failed = True
            continue
        identical = (baseline / f"{name}.yaml").read_bytes() == (current / f"{name}.yaml").read_bytes()
        print(f"{name}: {'BYTE_IDENTICAL' if identical else 'DIFFERENT'} sha256={c['sha256']} objects={c['objects']}")
        failed |= not identical
    print("PRODUCTION_RENDER_BYTE_IDENTICAL" if not failed else "PRODUCTION_RENDER_DIFFERS")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--ref", help="render the chart as committed at this Git ref instead of the working tree")
    c = sub.add_parser("compare")
    c.add_argument("baseline", type=Path)
    c.add_argument("current", type=Path)
    args = parser.parse_args(argv)
    if args.command == "compare":
        return compare(args.baseline, args.current)
    if args.ref:
        with tempfile.TemporaryDirectory() as tmp:
            summary = snapshot(args.out, chart_from_ref(args.ref, Path(tmp)), PRODUCTION_PROFILES, f"git:{args.ref}")
    else:
        summary = snapshot(args.out, ROOT / CHART_PATH, PRODUCTION_PROFILES, "working-tree")
    for name, profile in summary["profiles"].items():
        print(f"{name}: sha256={profile['sha256']} objects={profile['objects']} namespaces={profile['namespaces']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
