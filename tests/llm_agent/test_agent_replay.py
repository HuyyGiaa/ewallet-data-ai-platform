"""Owner evidence replay: verdicts (ops/llm_agent/replay_verdict.py) and the helper (scripts/llm_agent/agent_replay.sh).

Offline only: fake kubectl/helm/ss/uv and a fake /proc stand in for the cluster. Fixtures under
tests/fixtures/agent_replay are real responses and probe evidence from lc-20261117T002220Z (sanitized mirror).
Stdlib + pytest: uv run --frozen --project infra/docker/g3-byo-coordinator --group test python -m pytest
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import pytest

from ops.llm_agent import replay_verdict as rv

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests/fixtures/agent_replay"
SCRIPT = ROOT / "scripts/llm_agent/agent_replay.sh"
RUNBOOK = ROOT / "docs/edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md"
CTX = "gke_test_ctx"
MUTATING = re.compile(r"\b(apply|delete|patch|scale|annotate|label|create|edit|replace|rollout|cordon|drain|taint)\b")


def task(case: str) -> dict:
    return json.loads((FIX / f"{case}-task.json").read_text())


def result_of(body: dict) -> dict:
    return rv.structured_result(body)


def set_result(body: dict, mutate) -> dict:
    body = copy.deepcopy(body)
    for artifact in body["result"]["artifacts"]:
        for part in artifact["parts"]:
            try:
                value = json.loads(part.get("text") or "")
            except ValueError:
                continue
            if isinstance(value, dict) and "contract_version" in value:
                mutate(value)
                part["text"] = json.dumps(value)
    return body


def judge(tmp_path: Path, case: str, body, request_id: str | None = None) -> tuple[str, list[str]]:
    t, timing = tmp_path / "task.json", tmp_path / "timing.json"
    t.unlink(missing_ok=True)
    if body is not None:
        t.write_text(json.dumps(body))
    timing.write_text(json.dumps({"elapsed_seconds": 1.0}))
    verdict, reasons, _ = rv.e2e(case, t, timing, request_id)
    return verdict, reasons


# ------------------------------------------------------------------ e2e verdicts

REAL_IDS = {"fraud": "replay-fraud-013437", "drift": "replay-drift-013546", "combined": "replay-combined-013650",
            "nl-fraud": None, "nl-drift": None}


@pytest.mark.parametrize("case", rv.E2E_CASES)
def test_the_real_lc_responses_are_complete(tmp_path, case):
    assert judge(tmp_path, case, task(case), REAL_IDS[case]) == ("COMPLETE", [])


@pytest.mark.parametrize("case,mutate,expected", [
    ("fraud", lambda v: v.update(status="partial", unavailable=["fraud_scoring"]), "PARTIAL"),
    ("fraud", lambda v: v.update(status="unavailable", unavailable=["feature_lookup"]), "BACKEND_UNAVAILABLE"),
    ("fraud", lambda v: v.update(unavailable=["knowledge_search"]), "PARTIAL"),
    ("fraud", lambda v: v["fraud_score"].pop("fraud_probability"), "INVALID_RESPONSE"),
    ("fraud", lambda v: v.update(contract_version="g3-orchestration/v2"), "INVALID_RESPONSE"),
    ("drift", lambda v: v.update(status="partial"), "PARTIAL"),
    ("drift", lambda v: v["specialists"].pop("drift-specialist"), "MISSING_SPECIALIST"),
    ("drift", lambda v: v["specialists"]["drift-specialist"].update(status="unavailable", drift=[]), "PARTIAL"),
    ("drift", lambda v: v.update(status="unavailable"), "BACKEND_UNAVAILABLE"),
    ("drift", lambda v: v["specialists"]["drift-specialist"].update(drift=[]), "INVALID_RESPONSE"),
    ("combined", lambda v: v["specialists"].pop("feature-rag-specialist"), "MISSING_SPECIALIST"),
    ("combined", lambda v: v.update(unavailable=["drift-specialist"]), "PARTIAL"),
    ("combined", lambda v: v.update(workflow="drift"), "INVALID_RESPONSE"),
    ("nl-drift", lambda v: v.update(status="partial"), "PARTIAL"),
])
def test_partial_unavailable_and_incomplete_results_fail(tmp_path, case, mutate, expected):
    verdict, reasons = judge(tmp_path, case, set_result(task(case), mutate), REAL_IDS[case])
    assert verdict == expected and reasons


def test_task_state_alone_is_never_enough(tmp_path):
    body = task("fraud")
    failed = copy.deepcopy(body); failed["result"]["status"]["state"] = "failed"
    assert judge(tmp_path, "fraud", failed)[0] == "FAILED_TASK"
    canceled = copy.deepcopy(body); canceled["result"]["status"]["state"] = "canceled"
    assert judge(tmp_path, "fraud", canceled)[0] == "FAILED_TASK"
    text_only = copy.deepcopy(body)   # completed, HTTP 200, but no structured result
    text_only["result"]["artifacts"] = [{"parts": [{"kind": "text", "text": "looks fine"}]}]
    assert judge(tmp_path, "fraud", text_only)[0] == "INVALID_RESPONSE"
    assert judge(tmp_path, "fraud", {"jsonrpc": "2.0", "error": {"code": -32000}})[0] == "FAILED_TASK"
    assert judge(tmp_path, "fraud", None)[0] == "INVALID_RESPONSE"   # no response file
    assert judge(tmp_path, "fraud", body, "replay-fraud-other")[0] == "INVALID_RESPONSE"   # someone else's result


# ------------------------------------------------------------------ registry and negatives

def test_registry_needs_strict_pass_exit_zero_and_the_full_catalog(tmp_path):
    good = FIX / "registry-readback.json"
    assert not rv.registry(good, 0).failed
    assert rv.registry(good, 1).failed
    report = json.loads(good.read_text())
    report["entries"][0].update(matches=False, differences=["tag v1 != v2"]); report["verdict"] = "FAIL"
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(report))
    assert rv.registry(bad, 1).failed
    report = json.loads(good.read_text()); report["entries"] = report["entries"][:-1]
    short = tmp_path / "short.json"; short.write_text(json.dumps(report))
    assert rv.registry(short, 0).failed, "a missing Agent entry fails even if the rest matches"
    assert rv.registry(tmp_path / "missing.json", 0).failed
    (tmp_path / "broken.json").write_text("{")
    assert rv.registry(tmp_path / "broken.json", 0).failed


def neg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "neg"; d.mkdir(parents=True)
    for name in rv.NEGATIVES:
        (d / name).write_text((FIX / name).read_text())
    return d


ALL_ZERO = {name: 0 for name in rv.NEGATIVES}


def test_negatives_pass_only_with_every_denial_and_control(tmp_path):
    d = neg_dir(tmp_path)
    assert not rv.negative(d, ALL_ZERO).failed
    assert rv.negative(d, {**ALL_ZERO, "ungranted-tool.json": 1}).failed, "a failed probe fails the run"
    assert rv.negative(d, {k: v for k, v in ALL_ZERO.items() if k != "a2a-unrouted.json"}).failed
    evidence = json.loads((d / "wrong-server-credential.json").read_text())
    (d / "wrong-server-credential.json").write_text(json.dumps({**evidence, "verdict": "FAIL"}))
    assert rv.negative(d, ALL_ZERO).failed
    d2 = neg_dir(tmp_path / "x")
    evidence = json.loads((d2 / "ungranted-tool.json").read_text())
    (d2 / "ungranted-tool.json").write_text(json.dumps({**evidence, "control": {"outcome": "unauthenticated"}}))
    assert rv.negative(d2, ALL_ZERO).failed, "a denial without a working control proves nothing"
    d3 = neg_dir(tmp_path / "y"); (d3 / "a2a-unrouted.json").unlink()
    assert rv.negative(d3, ALL_ZERO).failed


# ------------------------------------------------------------------ status snapshot

def deploy(ns, name, ready, replicas=None, strategy=None):
    spec = {"replicas": replicas if replicas is not None else ready}
    if strategy:
        spec["strategy"] = strategy
    return {"metadata": {"namespace": ns, "name": name}, "spec": spec, "status": {"readyReplicas": ready}}


def cond(**kw):
    return {"conditions": [{"type": k, "status": v} for k, v in kw.items()]}


ROLLING = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}


def snapshot(tmp_path: Path, **override) -> Path:
    d = tmp_path / "snap"; d.mkdir(parents=True, exist_ok=True)
    files = {
        "helm-llm-agent.json": [{"revision": 59, "status": "deployed"}],
        "helm-llm-agent-mcp.json": [{"revision": 19, "status": "deployed"}],
        "deployments-ewallet-agents.json": {"items": [deploy("ewallet-agents", "fraud-coordinator", 2)] +
                                            [deploy("ewallet-agents", g, 1) for g in rv.GATEWAYS]},
        "deployments-ewallet-mcp.json": {"items": [deploy("ewallet-mcp", s, 2, strategy=ROLLING) for s in rv.MCP_SERVERS]},
        "pdb-ewallet-mcp.json": {"items": [{"metadata": {"name": s}} for s in rv.MCP_SERVERS]},
        "agents.json": {"items": [{"metadata": {"name": "fraud-coordinator"}, "spec": {"type": "BYO"},
                                   "status": cond(Ready="True", Accepted="True")}]},
        "sandboxagents.json": {"items": [{"metadata": {"name": n}, "status": cond(Ready="True", Accepted="True")}
                                         for n in rv.SANDBOX_AGENTS]},
        "workerpools.json": {"items": [{"metadata": {"name": "business-agents"}, "spec": {"replicas": 2},
                                        "status": {"replicas": 2}}]},
        "scaledobjects-ewallet-agents.json": {"items": [{"kind": "ScaledObject", "metadata": {"namespace": "ewallet-agents", "name": "fraud-coordinator"},
                                                         "spec": {"minReplicaCount": 2, "maxReplicaCount": 3},
                                                         "status": {**cond(Ready="True", Active="False", Paused="False"),
                                                                    "hpaName": "fraud-coordinator"}}]},
        "scaledobjects-ewallet-sandbox.json": {"items": [{"kind": "ScaledObject", "metadata": {"namespace": "ewallet-sandbox", "name": "business-agents-workerpool"},
                                                          "spec": {"minReplicaCount": 2, "maxReplicaCount": 3},
                                                          "status": {**cond(Ready="True", Active="False"),
                                                                     "hpaName": "business-agents-workerpool"}}]},
        "hpa-ewallet-agents.json": {"items": [{"metadata": {"namespace": "ewallet-agents", "name": "fraud-coordinator"},
                                               "status": {"currentReplicas": 2, "desiredReplicas": 2}}]},
        "hpa-ewallet-sandbox.json": {"items": [{"metadata": {"namespace": "ewallet-sandbox", "name": "business-agents-workerpool"},
                                                "status": {"currentReplicas": 2, "desiredReplicas": 2}}]},
        "pods-ewallet-agents.json": {"items": []}, "pods-ewallet-mcp.json": {"items": []}, "pods-ewallet-sandbox.json": {"items": []},
    }
    files.update(override)
    for name, value in files.items():
        (d / name).write_text(json.dumps(value))
    return d


def test_status_passes_on_the_sandbox_only_runtime_and_reports_retired_absence(tmp_path):
    report = rv.status(snapshot(tmp_path))
    assert not report.failed
    absent = [row for row in report.rows if row[1].startswith("retired")]
    assert len(absent) == 6 and all(row[0] == "PASS" and "absent" in row[2] for row in absent)


@pytest.mark.parametrize("override,failed", [
    ({"deployments-ewallet-agents.json": {"items": [deploy("ewallet-agents", "fraud-coordinator", 2),
                                                    deploy("ewallet-agents", "feature-rag-specialist", 2)] +
                                         [deploy("ewallet-agents", g, 1) for g in rv.GATEWAYS]}},
     "retired Deployment ewallet-agents/feature-rag-specialist absent"),
    ({"deployments-ewallet-mcp.json": {"items": [deploy("ewallet-mcp", "feature-rag-mcp", 1, 2, ROLLING),
                                                 deploy("ewallet-mcp", "drift-mcp", 2, strategy=ROLLING)]}},
     "MCP server ewallet-mcp/feature-rag-mcp"),
    ({"helm-llm-agent.json": [{"revision": 58, "status": "deployed"}]}, "Helm llm-agent"),
    ({"workerpools.json": {"items": []}}, "WorkerPool business-agents"),
    ({"sandboxagents.json": {"items": [{"metadata": {"name": "feature-rag-specialist"}, "status": cond(Ready="True")}]}},
     "SandboxAgent drift-specialist"),
    ({"agents.json": {"items": [{"metadata": {"name": n}, "spec": {"type": "BYO"}, "status": cond(Ready="True")}
                                for n in ("fraud-coordinator", "drift-specialist")]}}, "kagent Agents: only the BYO Coordinator"),
])
def test_status_fails_on_a_wrong_runtime(tmp_path, override, failed):
    assert failed in rv.status(snapshot(tmp_path, **override)).failed


def test_status_accepts_the_post_lf_revision_and_rejects_a_paused_scaledobject(tmp_path):
    report = rv.status(snapshot(tmp_path, **{"helm-llm-agent.json": [{"revision": 60, "status": "deployed"}]}))
    assert not report.failed and "LIVE_VERIFICATION_PENDING" in next(r[2] for r in report.rows if r[1] == "Helm llm-agent")
    paused = json.loads((snapshot(tmp_path / "p") / "scaledobjects-ewallet-sandbox.json").read_text())
    paused["items"][0]["metadata"]["annotations"] = {"autoscaling.keda.sh/paused-replicas": "2"}
    report = rv.status(snapshot(tmp_path / "q", **{"scaledobjects-ewallet-sandbox.json": paused}))
    assert "KEDA ScaledObject ewallet-sandbox/business-agents-workerpool" in report.failed


# ------------------------------------------------------------------ keda and workers

def write_verdicts(tmp_path: Path, verdicts: list[str]) -> list[Path]:
    paths = []
    for i, v in enumerate(verdicts):
        p = tmp_path / f"v{i}.json"; p.write_text(json.dumps({"verdict": v})); paths.append(p)
    return paths


def test_keda_claims_scaling_only_from_observed_replicas(tmp_path):
    line = lambda c, p: f"01:00:00Z coord={c} pool={p}"  # noqa: E731
    up_down = tmp_path / "t1"; up_down.write_text("\n".join(line(*s) for s in [(2, 2), (2, 3), (3, 3), (3, 3), (2, 2)]))
    assert not rv.keda(up_down, write_verdicts(tmp_path, ["COMPLETE"] * 6)).failed
    flat = tmp_path / "t2"; flat.write_text("\n".join(line(2, 2) for _ in range(8)))
    report = rv.keda(flat, write_verdicts(tmp_path, ["COMPLETE"] * 6))
    assert report.failed and any(r[0] == "NOT_OBSERVED" for r in report.rows)
    stuck = tmp_path / "t3"; stuck.write_text("\n".join(line(*s) for s in [(2, 2), (3, 3), (3, 3)]))
    assert rv.keda(stuck, write_verdicts(tmp_path, ["COMPLETE"] * 6)).failed, "no scale-in observed"
    assert rv.keda(up_down, write_verdicts(tmp_path, ["COMPLETE"] * 5 + ["PARTIAL"])).failed


def test_workers_read_the_real_load_row_format_and_measure_new_and_resume(tmp_path):
    sessions = tmp_path / "sessions.log"   # exactly what ops.experiments.sandbox_sessions prints
    rows = [{"agent": "drift-specialist", "phase": phase, "ok": True, "e2e_ms": ms, "state": "completed"}
            for phase, ms in (("new", 24500), ("resume", 23300), ("new", 25100), ("resume", 22800))]
    sessions.write_text("random log line\n" + "\n".join("LOAD_ROW " + json.dumps(r, sort_keys=True) for r in rows) + "\n")
    status = tmp_path / "status.jsonl"
    status.write_text("\n".join(json.dumps({"worker_pods": ["w-0", "w-1"], "actors": {"drift-specialist": [
        {"actor": "a1", "status": s, "pod": "w-0"}]}}) for s in ("RUNNING", "SUSPENDED", "RUNNING")) + "\n")
    golden = tmp_path / "golden.txt"; golden.write_text("")
    report = rv.workers(status, golden, sessions)
    assert not report.failed, report.rows
    text = {r[1]: (r[0], r[2]) for r in report.rows}
    assert text["Sandbox session turns executed"] == ("PASS", "4/4 ok")
    assert "median 24.8 s" in text["first response, new session, drift-specialist"][1]
    assert text["both new and resumed sessions measured"][0] == "PASS"
    assert text["an actor seen both Running and Suspended (suspend/resume)"][0] == "PASS"
    assert "not activation alone" in text["first response, resume session, drift-specialist"][1]
    not_sent = {"agent": "drift-specialist", "phase": "resume", "ok": False, "state": "not_sent"}
    sessions.write_text(sessions.read_text() + "LOAD_ROW " + json.dumps(not_sent) + "\n")
    assert rv.workers(status, golden, sessions).failed


def test_workers_separate_session_execution_from_placement(tmp_path):
    sessions = tmp_path / "sessions.log"
    sessions.write_text("\n".join(json.dumps({"id": f"c{i}", "ok": True}) for i in range(12)) + "\nnot json\n")
    rows = tmp_path / "status.jsonl"
    rows.write_text(json.dumps({"actors": {"feature-rag-specialist": [{"actor": "a1", "pod": "w-0"}, {"actor": "gold", "pod": "w-1"}],
                                           "drift-specialist": [{"actor": "b1", "pod": "w-0"}]}}) + "\n")
    golden = tmp_path / "golden.txt"; golden.write_text("gold\n")
    report = rv.workers(rows, golden, sessions)
    assert not report.failed and any(r[0] == "PASS" and "sharing" in r[1] for r in report.rows)
    sessions.write_text(sessions.read_text() + json.dumps({"id": "x", "ok": False}) + "\n")
    assert rv.workers(rows, golden, sessions).failed
    rows.write_text(json.dumps({"actors": {"feature-rag-specialist": [{"actor": "a1", "pod": "w-0"}],
                                           "drift-specialist": [{"actor": "b1", "pod": "w-1"}]}}) + "\n")
    sessions.write_text(json.dumps({"ok": True}) + "\n")
    report = rv.workers(rows, golden, sessions)
    assert not report.failed and any(r[0] == "NOT_OBSERVED" for r in report.rows), "placement varies; not a failure"


# ------------------------------------------------------------------ the shell helper against fakes

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import base64, json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("kubectl " + " ".join(args) + "\n")
fix = os.environ["FAKE_DIR"]
if args[:2] == ["config", "view"]:
    print(os.environ["FAKE_CTX"]); sys.exit(0)
if "logs" in args or "events" in args:
    sys.exit(0)
if "top" in args:   # kubectl top pods --no-headers
    ns = args[args.index("-n") + 1]
    pods = ["fraud-coordinator-a", "fraud-coordinator-b"] if ns == "ewallet-agents" else ["w-0", "w-1", "w-2"]
    print("\n".join(f"{p} 250m 300Mi" for p in pods)); sys.exit(0)
if any("goldenActorID" in a for a in args):
    print("golden-1\ngolden-2"); sys.exit(0)
if any("creationTimestamp" in a for a in args):   # worker pod samples: '|'-separated sequence, last repeats
    seq = os.path.join(fix, "workers.seq")
    values = open(seq).read().split("|") if os.path.exists(seq) else ["w-0=2026-11-20T00:00:00Z=2026-11-20T00:01:00Z=True "]
    print(values[0], end="")
    if len(values) > 1:
        open(seq, "w").write("|".join(values[1:]))
    sys.exit(0)
if "jsonpath={.spec.replicas}" in args:   # replica sequences for the keda demo (last value repeats)
    seq = os.path.join(fix, "coord.seq" if "fraud-coordinator" in args else "pool.seq")
    values = open(seq).read().split() if os.path.exists(seq) else ["2"]
    print(values[0], end="")
    if len(values) > 1:
        open(seq, "w").write(" ".join(values[1:]))
    sys.exit(0)
ns = args[args.index("-n") + 1] if "-n" in args else ""
get = args[args.index("get") + 1:] if "get" in args else []
what = get[0] if get else ""
names, skip = [], False
for a in get[1:]:
    if skip:
        skip = False
    elif a in ("-o", "-l", "--field-selector", "-n"):
        skip = True
    elif not a.startswith("-"):
        names.append(a)
joined = " ".join(args)
if os.environ.get("FAKE_FAIL") and os.environ["FAKE_FAIL"] in joined:
    print("Error from server (Forbidden)", file=sys.stderr); sys.exit(1)
if os.environ.get("FAKE_MALFORMED") and os.environ["FAKE_MALFORMED"] in joined and "json" in args:
    print("<html>proxy error</html>"); sys.exit(0)
if what == "secret":
    print(base64.b64encode(b"SUPER-SECRET-TOKEN-VALUE").decode(), end=""); sys.exit(0)
if what == "remotemcpservers.kagent.dev":
    print("mcp-token-kagent-controller-drift-mcp-r1-release1", end=""); sys.exit(0)
if what == "sandboxagents.kagent.dev" and names:
    print(json.dumps({"spec": {"image": "r.example/x/g3-byo-feature-rag-specialist@sha256:" + "a" * 64}})); sys.exit(0)
if "-o" in args and args[args.index("-o") + 1] == "json":
    key = {"deployments": f"deployments-{ns}.json", "pdb": f"pdb-{ns}.json", "agents.kagent.dev": "agents.json",
           "sandboxagents.kagent.dev": "sandboxagents.json", "workerpools.ate.dev": "workerpools.json",
           "scaledobjects.keda.sh": f"scaledobjects-{ns}.json", "hpa": f"hpa-{ns}.json", "pods": f"pods-{ns}.json"}.get(what)
    key = key or {"deploy": f"deployments-{ns}.json"}.get(what)
    if key and os.path.exists(os.path.join(fix, key)):
        data = json.load(open(os.path.join(fix, key)))
        if names:   # get <kind> <name> -o json: one object or NotFound
            items = data.get("items", []) if isinstance(data, dict) else data
            match = [i for i in items if i.get("metadata", {}).get("name") == names[0]]
            if not match:
                print(f'Error from server (NotFound): "{names[0]}" not found', file=sys.stderr); sys.exit(1)
            print(json.dumps(match[0])); sys.exit(0)
        print(json.dumps(data)); sys.exit(0)
    print(json.dumps({"items": []})); sys.exit(0)
if "deploy" in what and any(n in ("feature-rag-specialist", "drift-specialist") for n in names) and "--ignore-not-found" not in args:
    print('Error from server (NotFound): deployments.apps "feature-rag-specialist" not found', file=sys.stderr); sys.exit(1)
print("NAME   READY"); sys.exit(0)
'''

FAKE_HELM = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write("helm " + " ".join(args) + "\n")
if "history" in args and "json" in args:
    release = args[args.index("history") + 1]
    print(json.dumps([{"revision": 59 if release == "llm-agent" else 19, "status": "deployed"}]))
elif "get" in args and "values" in args:
    print("agents: {}")
else:
    print("REVISION STATUS\n59 deployed")
'''

# uv run ... python <script|-m module> ...: stands in for the probes and the registry readback.
FAKE_UV = r'''#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
log = open(os.environ["FAKE_LOG"], "a")
fix = os.environ["FAKE_FIX"]
def arg(name):
    return args[args.index(name) + 1] if name in args else None
if any("registry_catalog" in a for a in args):
    log.write("uv registry_catalog readback\n")
    report = json.load(open(os.path.join(fix, "registry-readback.json")))
    if os.environ.get("FAKE_REGISTRY") == "mismatch":
        report["entries"][1]["matches"] = False; report["entries"][1]["differences"] = ["tag differs"]; report["verdict"] = "FAIL"
    json.dump(report, open(arg("--evidence"), "w")); sys.exit(0 if report["verdict"] == "PASS" else 1)
script = next((a for a in args if a.endswith(".py")), "")
if script.endswith("agi_probe.py"):
    sub = args[args.index(script) + 1]
    if sub == "request":
        json.dump({"id": arg("--request-id"), "case": args[args.index(script) + 2]}, open(arg("--out"), "w")); sys.exit(0)
    evidence = arg("--evidence"); name = os.path.basename(evidence)
    token = os.environ.get("EWALLET_MCP_SERVICE_TOKEN")
    log.write(f"uv agi_probe {sub} {name} token_in_env={bool(token)} token_in_argv={'SUPER-SECRET' in ' '.join(args)}\n")
    shutil.copy(os.path.join(fix, name), evidence)
    sys.exit(1 if os.environ.get("FAKE_NEG_FAIL") == name else 0)
if script.endswith("g3_a2a_smoke.py"):
    request = json.load(open(arg("--request")))
    body = json.load(open(os.path.join(fix, request.get("case", os.environ.get("FAKE_CASE", "fraud")) + "-task.json")))
    for artifact in body["result"]["artifacts"]:
        for part in artifact["parts"]:
            try:
                value = json.loads(part["text"])
            except ValueError:
                continue
            value["request_id"] = request["id"]
            if os.environ.get("FAKE_E2E") == "partial":
                value["status"] = "partial"; value["unavailable"] = ["drift-specialist"]
            part["text"] = json.dumps(value)
    json.dump(body, open(arg("--response"), "w")); json.dump({"elapsed_seconds": 1.0}, open(arg("--timing"), "w"))
    log.write("uv g3_a2a_smoke\n"); sys.exit(0)
if "-c" in args:   # python -c 'import sys; print(sys.executable)'
    print(sys.executable); sys.exit(0)
if "ops.experiments.substrate_fleet" in args:   # actor samples: session actors on the added worker w-2
    log.write("uv substrate_fleet record\n")
    rows = [{"actors": {"feature-rag-specialist": [{"actor": "golden-1", "status": "Running", "pod": "w-2"},
                                                   {"actor": "a-1", "status": os.environ.get("FAKE_ACTOR_STATUS", "Running"), "pod": "w-2"},
                                                   {"actor": "a-2", "status": "Suspended", "pod": "w-0"}]},
             "worker_pods": ["w-0", "w-1", "w-2"]}]
    open(arg("--out"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n"); sys.exit(0)
log.write("uv UNEXPECTED " + " ".join(args) + "\n"); sys.exit(3)
'''

PORTS = {"18083": ("ewallet-agents", "svc/a2a-gateway", "18083:8083"), "18082": ("ewallet-agents", "svc/mcp-gateway", "18082:8080"),
         "18383": ("kagent", "svc/kagent-controller", "18383:8083"), "18300": ("kagent", "svc/kagent-ui", "18300:8080"),
         "13000": ("ewallet-agents", "svc/grafana", "13000:3000"),
         "18121": ("ewallet-prompts", "svc/prompt-registry-agentregistry", "18121:12121")}


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("helm", FAKE_HELM), ("uv", FAKE_UV)):
        (bin_dir / name).write_text(body); (bin_dir / name).chmod(0o755)
    # fake ss: the "listener" of port P is pid P; fake /proc/P/cmdline is this script's port-forward for P
    (bin_dir / "ss").write_text('#!/bin/sh\nport=$(echo "$*" | grep -oE \':[0-9]+\' | tr -d :)\n'
                                'echo "LISTEN 0 128 127.0.0.1:$port 0.0.0.0:* users:((\\"kubectl\\",pid=$port,fd=7))"\n')
    (bin_dir / "ss").chmod(0o755)
    proc = tmp_path / "proc"
    for port, (ns, target, ports) in PORTS.items():
        (proc / port).mkdir(parents=True)
        (proc / port / "cmdline").write_bytes("\0".join(["kubectl", "--context", CTX, "-n", ns, "port-forward", target, ports, ""]).encode())
    ev = tmp_path / "ev"; (ev / "inputs").mkdir(parents=True)
    (ev / "inputs/transaction.json").write_text('{"transaction_id": "dc58ff4e"}')
    agi_env = tmp_path / "campaign.env"
    agi_env.write_text(f"CTX={CTX}\nEV={ev}\nBASELINE=bl-r5gb-202604.account_tx_count_1h\n")
    fake_dir = snapshot(tmp_path)
    log = tmp_path / "calls.log"
    return {**os.environ, "AGI_ENV": str(agi_env), "KUBECTL": str(bin_dir / "kubectl"), "HELM": str(bin_dir / "helm"),
            "UV": str(bin_dir / "uv"), "SS": str(bin_dir / "ss"), "PROC_ROOT": str(proc), "FAKE_LOG": str(log),
            "FAKE_DIR": str(fake_dir), "FAKE_FIX": str(FIX), "FAKE_CTX": CTX, "REPLAY_DIR": str(tmp_path / "replay"),
            "PF_PIDS": str(tmp_path / "pf.pids"), "HOPS_SETTLE": "0", "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def run(env, *args, **extra):
    return subprocess.run(["bash", str(SCRIPT), *args], env={**env, **extra}, capture_output=True, text=True, timeout=120)


def calls(env) -> str:
    path = Path(env["FAKE_LOG"])
    return path.read_text() if path.exists() else ""


def test_status_needs_no_deleted_specialist_deployment_and_never_mutates(env):
    proc = run(env, "status")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "verdict: PASS" in proc.stdout and "retired Deployment ewallet-agents/drift-specialist absent" in proc.stdout
    log = calls(env)
    assert not re.search(r"get deploy[^\n]*(feature-rag-specialist|drift-specialist)", log)
    assert not [line for line in log.splitlines() if line.startswith("kubectl") and MUTATING.search(line)]


def test_status_fails_when_a_retired_workload_is_back(env, tmp_path):
    d = Path(env["FAKE_DIR"])
    data = json.loads((d / "deployments-ewallet-agents.json").read_text())
    data["items"].append(deploy("ewallet-agents", "drift-specialist", 2))
    (d / "deployments-ewallet-agents.json").write_text(json.dumps(data))
    proc = run(env, "status")
    assert proc.returncode != 0 and "PRESENT" in proc.stdout


def test_registry_mismatch_exits_non_zero_and_publishes_nothing(env):
    assert run(env, "registry").returncode == 0
    proc = run(env, "registry", FAKE_REGISTRY="mismatch")
    assert proc.returncode != 0 and "verdict: FAIL" in proc.stdout
    assert "publish" not in calls(env)


def test_negative_failures_propagate_and_tokens_never_leak(env, tmp_path):
    proc = run(env, "negative")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = calls(env)
    assert "token_in_env=True token_in_argv=False" in log and "SUPER-SECRET" not in log
    leaked = [p for p in Path(env["REPLAY_DIR"]).rglob("*") if p.is_file() and "SUPER-SECRET" in p.read_text(errors="ignore")]
    assert not leaked and "SUPER-SECRET" not in proc.stdout + proc.stderr
    failed = run(env, "negative", FAKE_NEG_FAIL="ungranted-tool.json", REPLAY_DIR=str(tmp_path / "r2"))
    assert failed.returncode != 0 and "verdict: FAIL" in failed.stdout


def test_e2e_fails_on_a_partial_result(env, tmp_path):
    ok = run(env, "e2e", "combined", FAKE_CASE="combined")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert '"verdict": "COMPLETE"' in ok.stdout
    bad = run(env, "e2e", "combined", FAKE_CASE="combined", FAKE_E2E="partial", REPLAY_DIR=str(tmp_path / "r2"))
    assert bad.returncode != 0 and '"verdict": "PARTIAL"' in bad.stdout


def test_request_ids_are_unique_under_concurrent_invocation(env):
    def one(_):
        return run(env, "__request-id", "fraud").stdout.strip()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(one, range(48)))
    assert len(set(ids)) == 48 and all(re.fullmatch(r"replay-fraud-\d{8}T\d{6}Z-\d+-[0-9a-f]{8}", i) for i in ids)
    assert all(len(i) + len(".fraud") <= 128 for i in ids)


@pytest.mark.parametrize("args,flag", [(("keda", "demo"), "REPLAY_LOAD_APPROVED"), (("workers",), "REPLAY_SESSIONS_APPROVED"),
                                       (("failover",), "REPLAY_EXPERIMENT_APPROVED"), (("autoscale",), "REPLAY_AUTOSCALE_APPROVED")])
def test_optional_load_sessions_and_experiments_need_their_own_approval(env, args, flag):
    proc = run(env, *args)
    assert proc.returncode != 0 and flag in proc.stderr
    assert not [line for line in calls(env).splitlines() if not line.startswith("kubectl config view")]


def test_keda_demo_runs_six_unique_requests_and_judges_observed_replicas(env, tmp_path):
    d = Path(env["FAKE_DIR"])
    (d / "coord.seq").write_text("2 2 3 3 3 3 2 2")
    (d / "pool.seq").write_text("2 3 3 3 3 2 2 2")
    proc = run(env, "keda", "demo", REPLAY_LOAD_APPROVED="yes", FAKE_CASE="combined", KEDA_SAMPLE="0.3",
               KEDA_POLL="1", KEDA_COOLDOWN="0", SCALE_IN_WAIT="20")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr
    verdicts = list(Path(env["REPLAY_DIR"]).glob("keda/job*/e2e/*-verdict.json"))
    ids = {json.loads(v.read_text())["request_id"] for v in verdicts}
    assert len(verdicts) == 6 and len(ids) == 6, "six requests, six distinct ids"
    assert "6/6 COMPLETE" in proc.stdout and "verdict: PASS" in proc.stdout
    assert not [line for line in calls(env).splitlines() if line.startswith("kubectl") and MUTATING.search(line)]
    (d / "coord.seq").write_text("2"); (d / "pool.seq").write_text("2")
    flat = run(env, "keda", "demo", REPLAY_LOAD_APPROVED="yes", FAKE_CASE="combined", KEDA_SAMPLE="0.3",
               KEDA_POLL="1", KEDA_COOLDOWN="0", SCALE_IN_WAIT="5", REPLAY_DIR=str(tmp_path / "r2"))
    assert flat.returncode != 0 and "NOT_OBSERVED" in flat.stdout


@pytest.mark.parametrize("extra,failed", [
    ({"FAKE_FAIL": "scaledobjects.keda.sh fraud-coordinator"}, "query scaledobject-coordinator"),
    ({"FAKE_FAIL": "scaledobjects.keda.sh business-agents-workerpool"}, "query scaledobject-workerpool"),
    ({"FAKE_FAIL": "-n ewallet-sandbox get hpa -o json"}, "query hpa-ewallet-sandbox"),
    ({"FAKE_MALFORMED": "get workerpools.ate.dev business-agents"}, "query workerpool"),
])
def test_keda_status_fails_on_a_failed_or_malformed_observation(env, extra, failed):
    proc = run(env, "keda", **extra)
    assert proc.returncode != 0 and failed in proc.stdout, proc.stdout + proc.stderr


def test_keda_status_fails_on_a_missing_object_or_replica_count(env):
    d = Path(env["FAKE_DIR"])
    so = json.loads((d / "scaledobjects-ewallet-agents.json").read_text())
    (d / "scaledobjects-ewallet-agents.json").write_text(json.dumps({"items": []}))
    proc = run(env, "keda")
    assert proc.returncode != 0 and "query scaledobject-coordinator" in proc.stdout
    (d / "scaledobjects-ewallet-agents.json").write_text(json.dumps(so))
    pools = json.loads((d / "workerpools.json").read_text()); pools["items"][0]["status"] = {}
    (d / "workerpools.json").write_text(json.dumps(pools))
    proc = run(env, "keda")
    assert proc.returncode != 0 and "WorkerPool replicas" in proc.stdout
    hpas = {"items": []}
    (d / "workerpools.json").write_text(json.dumps({"items": [{"metadata": {"name": "business-agents"},
                                                               "spec": {"replicas": 2}, "status": {"replicas": 2}}]}))
    (d / "hpa-ewallet-sandbox.json").write_text(json.dumps(hpas))
    proc = run(env, "keda")
    assert proc.returncode != 0 and "HPA owned by ewallet-sandbox/business-agents-workerpool" in proc.stdout


def test_keda_status_never_claims_scaling_from_ready(tmp_path):
    d = tmp_path / "k"; d.mkdir()
    snap = snapshot(tmp_path)
    so = lambda name, ns: next(i for i in json.loads((snap / f"scaledobjects-{ns}.json").read_text())["items"])  # noqa: E731
    files = {"scaledobject-coordinator": so("fraud-coordinator", "ewallet-agents") | {"kind": "ScaledObject"},
             "scaledobject-workerpool": so("business-agents-workerpool", "ewallet-sandbox") | {"kind": "ScaledObject"},
             "hpa-ewallet-agents": json.loads((snap / "hpa-ewallet-agents.json").read_text()),
             "hpa-ewallet-sandbox": json.loads((snap / "hpa-ewallet-sandbox.json").read_text()),
             "deploy-coordinator": deploy("ewallet-agents", "fraud-coordinator", 2),
             "workerpool": json.loads((snap / "workerpools.json").read_text())["items"][0]}
    for name, value in files.items():
        (d / f"{name}.json").write_text(json.dumps(value))
    (d / "queries.tsv").write_text("".join(f"{n}\t0\n" for n in files))
    report = rv.keda_status(d)
    assert not report.failed
    assert any(r[0] == "INFO" and "not assessed" in r[2] for r in report.rows)
    assert not any("scale-out" in r[1] and r[0] == "PASS" for r in report.rows)
    (d / "queries.tsv").write_text("".join(f"{n}\t0\n" for n in list(files)[:-1]))
    assert "query workerpool" in rv.keda_status(d).failed, "an observation that was never made fails"


def test_worker_startup_is_measured_only_for_pods_added_during_the_run(tmp_path):
    pods = tmp_path / "worker-pods.txt"
    pods.write_text("01:00:00Z w-0=2026-11-20T00:00:00Z=2026-11-20T00:01:00Z=True w-1=2026-11-20T00:00:00Z=2026-11-20T00:01:05Z=True\n"
                    "01:00:10Z w-0=2026-11-20T00:00:00Z=2026-11-20T00:01:00Z=True w-2=2026-11-20T01:00:05Z==False\n"
                    "01:00:40Z w-0=2026-11-20T00:00:00Z=2026-11-20T00:01:00Z=True w-2=2026-11-20T01:00:05Z=2026-11-20T01:00:24Z=True\n")
    assert rv.worker_startups(pods) == {"w-2": 19.0}


def test_keda_default_is_read_only(env):
    proc = run(env, "keda")
    assert proc.returncode == 0 and "not a scale-out result" in proc.stdout
    log = calls(env)
    assert "uv " not in log and "agent_experiment" not in log
    assert not [line for line in log.splitlines() if line.startswith("kubectl") and MUTATING.search(line)]


def free_port(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def test_pf_refuses_a_foreign_listener_and_kills_nothing(env, tmp_path):
    if not free_port(18083):
        pytest.skip("127.0.0.1:18083 is in use on this machine")
    listener = subprocess.Popen([sys.executable, "-c", "import socket,time;s=socket.socket();"
                                 "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',18083));s.listen();time.sleep(60)"])
    try:
        for _ in range(50):
            if not free_port(18083):
                break
            time.sleep(0.1)
        (tmp_path / "bin/ss").write_text(f'#!/bin/sh\necho "LISTEN 0 128 127.0.0.1:18083 users:((\\"python3\\",pid={listener.pid},fd=3))"\n')
        proc = run(env, "pf", PROC_ROOT="/proc", PF_SETTLE="0")
        assert proc.returncode != 0
        assert "127.0.0.1:18083  held by another process" in proc.stdout and "nothing was killed" in proc.stdout
        assert listener.poll() is None, "the foreign listener must survive"
        e2e = run(env, "e2e", "fraud", PROC_ROOT="/proc", FAKE_CASE="fraud")
        assert e2e.returncode != 0 and "not this script's port-forward" in e2e.stderr
    finally:
        listener.kill()


def test_stop_pf_kills_only_its_own_port_forwards(env, tmp_path):
    # A process whose command line is this script's port-forward for 18083 (argv carries the same words).
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             "kubectl", "--context", CTX, "-n", "ewallet-agents", "port-forward", "svc/a2a-gateway", "18083:8083"])
    unrelated = subprocess.Popen(["sleep", "60"])
    old_style = subprocess.Popen(["sleep", "61"])
    try:
        Path(env["PF_PIDS"]).write_text(f"{ours.pid} 18083 ewallet-agents svc/a2a-gateway 18083:8083\n"
                                        f"{unrelated.pid} 18082 ewallet-agents svc/mcp-gateway 18082:8080\n{old_style.pid}\n")
        proc = run(env, "stop-pf", PROC_ROOT="/proc")
        assert proc.returncode == 0, proc.stderr
        ours.wait(timeout=5)
        assert unrelated.poll() is None and old_style.poll() is None, "unrelated processes survive"
        assert f"skipped {unrelated.pid}" in proc.stdout and f"skipped {old_style.pid}" in proc.stdout
    finally:
        for p in (ours, unrelated, old_style):
            p.kill()


# ------------------------------------------------------------------ the runbook

def test_runbook_required_steps_name_no_retired_resource_or_stale_procedure():
    text = RUNBOOK.read_text()
    required = text.split("## Reference: history", 1)[0]
    for stale in ("sleep_20261106T163042Z --nodes", "scale deploy/feature-rag-mcp", "3 Agents `BYO`",
                  "3 ScaledObjects", "feature-rag-specialist)'", "annotate scaledobject", "not implemented yet",
                  "has no span", "registry_catalog publish", "mcp-token-drift-specialist", "mcp-token-feature-rag-specialist"):
        assert stale not in required, stale
    assert "lc-20261117T002220Z/final-sleep/recovery-manifest" in required and "--nodes 5" in required
    assert "business-agents-workerpool" in required and "kagent-controller" in required
    assert "REPLAY_EXPERIMENT_APPROVED" in required and "LIVE_VERIFICATION_PENDING" in required


def test_runbook_cloud_lab_and_helper_commands_exist():
    text = RUNBOOK.read_text()
    cloud_lab = (ROOT / "ops/gcp/cloud_lab.py").read_text()
    for command in set(re.findall(r"cloud_lab\.py (?:--kubeconfig \S+ )?([a-z-]+)", text)):
        assert f'"{command}"' in cloud_lab, command
    for flag in set(re.findall(r"cloud_lab\.py [^\n]*", text)):
        for name in re.findall(r"(--[a-z-]+)", flag):
            assert f'"{name}"' in cloud_lab, name
    helper = SCRIPT.read_text()
    for command in set(re.findall(r"agent_replay\.sh ([a-z][a-z0-9-]*)", text)):
        assert re.search(rf"^\s+{re.escape(command)}\)", helper, re.M), command


def test_runbook_screenshot_vocabulary_is_defined_once_and_has_no_historical_substitute():
    """Every screenshot named in the runbook phases is defined once in the checklist; no H (historical) class."""
    runbook = RUNBOOK.read_text()
    body, rest = runbook.split("## Screenshot checklist", 1)
    checklist = rest.split("## Reference: history", 1)[0]
    defined = re.findall(r"^\| `(AGENT_[A-H]\d{2}_[A-Z0-9_]+\.png)` \| ([MC]) \|", checklist, re.M)
    names = [name for name, _ in defined]
    assert len(names) == len(set(names)) and names, "each screenshot is defined once"
    assert set(re.findall(r"AGENT_[A-H]\d{2}_[A-Z0-9_]+\.png", body)) == set(names), "phases and checklist agree"
    assert not re.search(r"^\| [^|]+ \| H \|", checklist, re.M), "no historical row stands in for a screenshot"
    classes = dict(defined)
    for name in ("AGENT_D02_GRAFANA_KEDA_SIGNALS.png", "AGENT_D03_COORDINATOR_SCALEOUT.png", "AGENT_D04_WORKERPOOL_SCALEOUT.png",
                 "AGENT_D05_THIRD_WORKER_ACTORS.png", "AGENT_D06_AUTOSCALE_VERDICT.png", "AGENT_D07_RESOURCE_USAGE.png",
                 "AGENT_E01_FLEET_SESSIONS.png"):
        assert classes.get(name) == "M", name
    assert not re.search(r"AGENT_\d{2}", runbook), "no screenshot names from the earlier scheme"


def test_runbook_makes_b_prime_the_single_primary_autoscaling_replay():
    text = RUNBOOK.read_text()
    required = text.split("## Reference: history", 1)[0]
    assert "REPLAY_AUTOSCALE_APPROVED=yes scripts/llm_agent/agent_replay.sh autoscale" in required
    assert "100 typed business tasks, concurrency 8, at most\n900 s" in required
    assert "REPLAY_LOAD_APPROVED=yes scripts/llm_agent/agent_replay.sh keda demo" not in required, "demo is not in the sequence"
    assert "export REPLAY_DIR=" in required and required.count("export REPLAY_DIR=") == 1
    assert "~/ewallet-evidence/agents/replay/$RC-recovery-manifest" not in required
    # --also-approve only from not_covered: no fixed PDB on the sleep commands
    for line in re.findall(r"cloud_lab\.py sleep [^\n]*(?:\n  [^\n]*)*", required):
        assert "--also-approve ewallet-mcp" not in line, line
    assert '"${ALSO[@]}"' in required and "not_covered" in required
    assert '--manifest "$P"' in required and 'verify --expect sleep --manifest "$REPLAY_DIR/recovery-manifest"' in required
    assert "agent_experiment.sh exp-c5-keda optimized run \"$REPLAY_DIR/agr06\"" not in required


def test_evidence_commands_need_one_campaign_replay_dir(env):
    proc = run(env, "status", REPLAY_DIR="")
    assert proc.returncode != 0 and "set REPLAY_DIR once" in proc.stderr
    assert not [line for line in calls(env).splitlines() if not line.startswith("kubectl config view")]


FAKE_DRIVER = r'''#!/usr/bin/env bash
# Stands in for scripts/experiments/agent_experiment.sh: records its arguments and profile, writes a load summary.
set -eu
echo "$* concurrency=$EXP_CONCURRENCY max=$EXP_MAX_REQUESTS duration=$EXP_DURATION scale_in_wait=$EXP_SCALE_IN_WAIT approved=$EXP_APPROVED ctx=$KUBE_CONTEXT" >> "$FAKE_LOG.driver"
sleep "${FAKE_LOAD_SECONDS:-4}"   # the samplers read the replica sequences while the load runs
run="$4/optimized/raw/run-01"; mkdir -p "$run"
ok=${FAKE_LOAD_OK:-100}
cat > "$run/summary.json" <<EOF
{"levels": {"load": {"requests": 100, "succeeded": $ok, "availability_percent": $ok.0, "states": {"completed": $ok},
 "e2e_ms": {"p50": 34040.9, "p95": 102556.1, "mean": 41517.6}, "requests_per_second": 0.1855, "status": "MEASURED"}}}
EOF
echo "run 01 complete: $run"
exit "${FAKE_LOAD_RC:-0}"
'''


def autoscale_env(env, tmp_path, coord="2 2 3 3 3 2 2", pool="2 3 3 3 3 2 2"):
    d = Path(env["FAKE_DIR"])
    (d / "coord.seq").write_text("2 " + coord)   # the first read is the pre-load baseline check
    (d / "pool.seq").write_text("2 " + pool)
    first = "w-0=2026-11-20T00:00:00Z=2026-11-20T00:01:00Z=True w-1=2026-11-20T00:00:00Z=2026-11-20T00:01:05Z=True "
    added = first + "w-2=2026-11-20T01:00:05Z=2026-11-20T01:00:24Z=True "
    (d / "workers.seq").write_text("|".join([first, added, added, first]))
    driver = tmp_path / "fake_driver.sh"; driver.write_text(FAKE_DRIVER); driver.chmod(0o755)
    return {"REPLAY_AUTOSCALE_APPROVED": "yes", "AUTOSCALE_DRIVER": str(driver), "AUTOSCALE_SAMPLE": "0.3",
            "AUTOSCALE_TOP_INTERVAL": "0.3", "AUTOSCALE_ACTOR_INTERVAL": "1", "AUTOSCALE_COOLDOWN": "0", "AUTOSCALE_POLL": "1",
            "AUTOSCALE_SCALE_IN_WAIT": "6"}


def test_autoscale_replays_b_prime_and_judges_both_autoscalers_and_the_added_worker(env, tmp_path):
    proc = run(env, "autoscale", **autoscale_env(env, tmp_path))
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr
    driver = Path(env["FAKE_LOG"] + ".driver").read_text()
    assert "exp-c5-keda optimized run" in driver and "/autoscale/load" in driver
    assert "concurrency=8 max=100 duration=900 scale_in_wait=0 approved=yes" in driver
    out = proc.stdout
    assert "Coordinator (ewallet-agents/fraud-coordinator) 2 -> 3 -> 2" in out and "WorkerPool (ewallet-sandbox/business-agents) 2 -> 3 -> 2" in out
    assert "the added worker executed Sandbox actors  (w-2: 1 Running)" in out, "golden actors are excluded"
    assert "100/100 tasks completed" in out and "p95 102.6 s" in out and "w-2 19 s" in out
    assert "peak usage ewallet-sandbox/w-2" in out and "verdict: PASS" in out
    replay = Path(env["REPLAY_DIR"]) / "autoscale"
    for name in ("before/verdict.json", "after/verdict.json", "timeline.txt", "worker-pods.txt", "actors.jsonl", "top.txt", "verdict.json"):
        assert (replay / name).exists(), name
    assert not [line for line in calls(env).splitlines() if line.startswith("kubectl") and MUTATING.search(line)]
    again = run(env, "autoscale", **autoscale_env(env, tmp_path))
    assert again.returncode != 0 and "one primary autoscaling run per REPLAY_DIR" in again.stderr


@pytest.mark.parametrize("override,extra,expected", [
    ({"coord": "2 2 2 2 2 2"}, {}, "NOT_OBSERVED"),                     # Coordinator never scaled
    ({"pool": "2 3 3 3 3 3"}, {}, "NOT_OBSERVED"),                      # WorkerPool never returned to 2
    ({}, {"FAKE_ACTOR_STATUS": "Suspended"}, "no Running actor seen on an added worker"),
    ({}, {"FAKE_LOAD_OK": "97"}, "97/100 tasks completed"),
    ({}, {"FAKE_LOAD_RC": "1"}, "load harness exit code"),
])
def test_autoscale_fails_when_any_part_is_not_observed(env, tmp_path, override, extra, expected):
    proc = run(env, "autoscale", **autoscale_env(env, tmp_path, **override), **extra)
    assert proc.returncode != 0 and expected in proc.stdout, proc.stdout[-3000:]
    assert "verdict: FAIL" in proc.stdout


def test_autoscale_sends_no_load_unless_keda_is_healthy_and_at_rest(env, tmp_path):
    proc = run(env, "autoscale", **autoscale_env(env, tmp_path), FAKE_FAIL="scaledobjects.keda.sh fraud-coordinator")
    assert proc.returncode != 0 and "no load was sent" in proc.stderr
    assert not Path(env["FAKE_LOG"] + ".driver").exists()
    d = Path(env["FAKE_DIR"])
    extra = autoscale_env(env, tmp_path); (d / "coord.seq").write_text("3")
    busy = run(env, "autoscale", **extra, REPLAY_DIR=str(tmp_path / "r2"))
    assert busy.returncode != 0 and "both autoscalers must start at 2" in busy.stderr
    assert not Path(env["FAKE_LOG"] + ".driver").exists()
