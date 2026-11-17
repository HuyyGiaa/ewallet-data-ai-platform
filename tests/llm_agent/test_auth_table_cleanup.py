"""Obsolete MCP auth-table cleanup: matrix rules, token-free inventory and the retired-credential guard.

Stdlib only (no cluster): run with `uv run --frozen --project infra/docker/g3-byo-coordinator --group test python -m pytest`.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from ops.llm_agent import auth_table_inventory as inv
from ops.llm_agent import auth_table_matrix as m
from ops.llm_agent import retired_credentials as rc

ROOT = Path(__file__).resolve().parents[2]


def table(ns, name, *subjects, immutable=True, status="active"):
    return {"namespace": ns, "name": name, "immutable": immutable,
            "credentials": [{"id": f"{s}.x.{status}", "subject": s, "status": status} for s in subjects]}


def inventory(agents_drift_present=False):
    return {
        "tables": [
            table("ewallet-mcp", "mcp-auth-drift-mcp-fin-20261117", "kagent-controller", "sandbox-drift"),
            table("ewallet-mcp", "mcp-auth-feature-rag-mcp-fin-20261117", "kagent-controller", "sandbox-feature-rag"),
            table("ewallet-mcp", "mcp-auth-drift-mcp-r1-transition", "drift-specialist", "kagent-controller"),
            table("ewallet-mcp", "mcp-auth-drift-mcp-sbx-20261115", "drift-specialist", "sandbox-drift"),
            table("ewallet-mcp", "mcp-auth-feature-rag-mcp-r1-transition", "feature-rag-specialist"),
            table("ewallet-mcp", "mcp-auth-feature-rag-mcp-sbx-20261115", "feature-rag-specialist", "sandbox-feature-rag"),
            table("ewallet-agents", "mcp-auth-drift-mcp", "drift-specialist"),
            table("ewallet-agents", "mcp-auth-feature-rag-mcp", "drift-specialist", "feature-rag-specialist"),
            table("ewallet-agents", "mcp-auth-drift-mcp-fin-20261117", "kagent-controller"),
            table("ewallet-agents", "mcp-auth-drift-mcp-fin-20261117-b", "sandbox-drift"),
        ],
        "servers": {
            "ewallet-mcp/drift-mcp": {"present": True, "table": "mcp-auth-drift-mcp-fin-20261117"},
            "ewallet-mcp/feature-rag-mcp": {"present": True, "table": "mcp-auth-feature-rag-mcp-fin-20261117"},
            "ewallet-agents/drift-mcp": {"present": agents_drift_present,
                                         "table": "mcp-auth-drift-mcp-fin-20261117-b" if agents_drift_present else None},
            "ewallet-agents/feature-rag-mcp": {"present": False, "table": None},
        },
        "references": {
            "live": {"ewallet-mcp/mcp-auth-drift-mcp-fin-20261117": ["Pod/ewallet-mcp/drift-mcp-a"]},
            "history": {"ewallet-mcp/mcp-auth-drift-mcp-sbx-20261115": ["ReplicaSet/ewallet-mcp/drift-mcp-old"]},
            "helm_current": {"ewallet-mcp/mcp-auth-drift-mcp-fin-20261117": ["llm-agent-mcp"]},
            "helm_history": {"ewallet-mcp/mcp-auth-drift-mcp-sbx-20261115": ["llm-agent-mcp@17"],
                             "ewallet-agents/mcp-auth-drift-mcp": ["llm-agent@57"]},
        },
    }


REVIEWED = sorted(["ewallet-mcp/mcp-auth-drift-mcp-r1-transition", "ewallet-mcp/mcp-auth-drift-mcp-sbx-20261115",
                   "ewallet-mcp/mcp-auth-feature-rag-mcp-r1-transition", "ewallet-mcp/mcp-auth-feature-rag-mcp-sbx-20261115",
                   "ewallet-agents/mcp-auth-drift-mcp", "ewallet-agents/mcp-auth-feature-rag-mcp"])


def decisions(matrix):
    return {f"{r['namespace']}/{r['name']}": r["decision"] for r in matrix["rows"]}


def test_only_the_obsolete_unreferenced_sets_are_deleted_and_final_sets_kept():
    matrix = m.build(inventory())
    assert matrix["problems"] == [] and matrix["delete"] == REVIEWED
    d = decisions(matrix)
    for kept in ("ewallet-mcp/mcp-auth-drift-mcp-fin-20261117", "ewallet-mcp/mcp-auth-feature-rag-mcp-fin-20261117",
                 "ewallet-agents/mcp-auth-drift-mcp-fin-20261117-b", "ewallet-agents/mcp-auth-drift-mcp-fin-20261117"):
        assert d[kept] == "KEEP", kept
    row = next(r for r in matrix["rows"] if r["name"] == "mcp-auth-drift-mcp-sbx-20261115")
    assert row["historical_helm_revisions"] == ["llm-agent-mcp@17"] and row["history_references"]
    assert row["retired_subjects"] == ["drift-specialist"]


def test_the_legacy_server_before_retirement_reads_a_verified_final_table():
    assert m.build(inventory(agents_drift_present=True))["delete"] == REVIEWED


def test_a_live_reference_keeps_the_table():
    data = inventory()
    data["references"]["live"]["ewallet-mcp/mcp-auth-drift-mcp-r1-transition"] = ["Pod/ewallet-mcp/x"]
    assert decisions(m.build(data))["ewallet-mcp/mcp-auth-drift-mcp-r1-transition"] == "UNKNOWN"


def test_a_current_helm_reference_keeps_the_table():
    data = inventory()
    data["references"]["helm_current"]["ewallet-agents/mcp-auth-drift-mcp"] = ["llm-agent"]
    assert decisions(m.build(data))["ewallet-agents/mcp-auth-drift-mcp"] == "UNKNOWN"


def test_a_live_server_on_an_obsolete_set_blocks_every_deletion():
    data = inventory()
    data["servers"]["ewallet-mcp/drift-mcp"]["table"] = "mcp-auth-drift-mcp-sbx-20261115"
    matrix = m.build(data)
    assert matrix["problems"] and matrix["delete"] == []


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(immutable=False),
    lambda t: t["credentials"].append({"id": "d", "subject": "drift-specialist", "status": "active"}),
    lambda t: t.update(credentials=[{"id": "x", "subject": "sandbox-drift", "status": "revoked"}]),
    lambda t: t.update(credentials=None),
])
def test_an_unverified_final_table_blocks_every_deletion(mutate):
    data = inventory()
    mutate(data["tables"][0])
    matrix = m.build(data)
    assert matrix["problems"] and matrix["delete"] == []
    assert "UNKNOWN" in set(decisions(matrix).values())


def test_unknown_names_and_non_obsolete_sets_are_kept():
    data = inventory()
    data["tables"] += [table("ewallet-mcp", "mcp-auth-other-thing", "x"), table("ewallet-mcp", "mcp-auth-drift-mcp-rot-1", "x")]
    d = decisions(m.build(data))
    assert d["ewallet-mcp/mcp-auth-other-thing"] == "UNKNOWN" and d["ewallet-mcp/mcp-auth-drift-mcp-rot-1"] == "UNKNOWN"


def test_main_fails_when_the_computed_list_differs_from_the_reviewed_list(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory()))
    assert m.main(["--inventory", str(path), "--out", str(tmp_path / "m.json"), "--expect-delete", *REVIEWED]) == 0
    assert m.main(["--inventory", str(path), "--out", str(tmp_path / "m.json"), "--expect-delete", *REVIEWED[:-1]]) == 1
    assert (tmp_path / "m.tsv").read_text().startswith("decision\t")


def test_collector_drops_tokens_and_classifies_references():
    secret_data = {"version": 1, "credentials": [{"id": "sandbox-drift.drift-mcp.fin.active", "subject": "sandbox-drift",
                                                  "token": "SUPER-SECRET-VALUE", "status": "active"}]}

    def secret(ns, name):
        return {"metadata": {"namespace": ns, "name": name}, "immutable": True,
                "data": {"auth-config.json": base64.b64encode(json.dumps(secret_data).encode()).decode()}}

    def deployment(ns, name, tbl):
        env = [{"name": "EWALLET_MCP_SERVICE_TOKENS", "valueFrom": {"secretKeyRef": {"name": tbl, "key": "auth-config.json"}}}]
        return {"kind": "Deployment", "metadata": {"namespace": ns, "name": name},
                "spec": {"replicas": 2, "template": {"spec": {"containers": [{"env": env}]}}}}

    live_deploy = deployment("ewallet-mcp", "drift-mcp", "mcp-auth-drift-mcp-fin-20261117")
    old_rs = {"kind": "ReplicaSet", "metadata": {"namespace": "ewallet-mcp", "name": "drift-mcp-old"},
              "spec": {"replicas": 0, "template": {"spec": {"containers": [{"env": [{"valueFrom": {"secretKeyRef": {
                  "name": "mcp-auth-drift-mcp-sbx-20261115"}}}]}]}}}}

    def run(argv):
        joined = " ".join(argv)
        if "get secrets" in joined:
            ns = argv[argv.index("-n") + 1]
            names = {"ewallet-mcp": ["mcp-auth-drift-mcp-fin-20261117", "mcp-auth-drift-mcp-sbx-20261115", "other"],
                     "ewallet-agents": []}[ns]
            return json.dumps({"items": [secret(ns, n) for n in names]})
        if "get deployments --field-selector" in joined:
            present = "-n ewallet-mcp" in joined and "metadata.name=drift-mcp" in joined
            return json.dumps({"items": [live_deploy] if present else []})
        if "api-resources" in joined:
            return "deployments.apps\nremotemcpservers.kagent.dev\nsandboxagents.agents.x-k8s.io\nconfigmaps\n"
        if " get " in joined and "-A" in argv:
            kind = argv[argv.index("get") + 1]
            return json.dumps({"items": {"deployments": [live_deploy], "replicasets": [old_rs]}.get(kind, [])})
        if "history" in argv:
            return json.dumps([{"revision": 17}, {"revision": 18}])
        if "manifest" in argv:
            return "name: mcp-auth-drift-mcp-sbx-20261115" if "17" in argv else "name: mcp-auth-drift-mcp-fin-20261117"
        raise AssertionError(joined)

    data = inv.collect("ctx", [("llm-agent-mcp", "ewallet-mcp")], run=run)
    text = json.dumps(data)
    assert "SUPER-SECRET-VALUE" not in text and "token" not in text
    assert {t["name"] for t in data["tables"]} == {"mcp-auth-drift-mcp-fin-20261117", "mcp-auth-drift-mcp-sbx-20261115"}
    assert "remotemcpservers.kagent.dev" in data["scanned_kinds"] and "configmaps" not in data["scanned_kinds"]
    refs = data["references"]
    assert refs["live"]["ewallet-mcp/mcp-auth-drift-mcp-fin-20261117"] == ["Deployment/ewallet-mcp/drift-mcp"]
    assert refs["history"]["ewallet-mcp/mcp-auth-drift-mcp-sbx-20261115"] == ["ReplicaSet/ewallet-mcp/drift-mcp-old"]
    assert refs["helm_history"]["ewallet-mcp/mcp-auth-drift-mcp-sbx-20261115"] == ["llm-agent-mcp@17"]
    assert data["servers"]["ewallet-mcp/drift-mcp"] == {"present": True, "table": "mcp-auth-drift-mcp-fin-20261117"}


def test_delete_needs_the_campaign_approval_before_touching_the_cluster(tmp_path):
    log = tmp_path / "calls"
    fake = tmp_path / "kubectl"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    fake.chmod(0o755)
    env = dict(os.environ, CTX="ctx", LC_CAMPAIGN="lc-x", EXPECT_DELETE=" ".join(REVIEWED), KUBECTL=str(fake),
               LC_APPROVAL="AUTH-TABLES:other-campaign")
    proc = subprocess.run(["bash", str(ROOT / "scripts/llm_agent/mcp_auth_tables.sh"), "delete", str(tmp_path / "out")],
                          env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "AUTH-TABLES:lc-x" in proc.stderr
    assert not log.exists()
    env.update(LC_APPROVAL="AUTH-TABLES:lc-x", EXPECT_DELETE="ewallet-mcp/some-other-secret")
    proc = subprocess.run(["bash", str(ROOT / "scripts/llm_agent/mcp_auth_tables.sh"), "delete", str(tmp_path / "out")],
                          env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "not an MCP auth table" in proc.stderr and not log.exists()


# ------------------------------------------------------------------ retired-credential guard

@pytest.mark.parametrize("name,retired", [
    ("mcp-auth-drift-mcp", True),
    ("mcp-auth-feature-rag-mcp-sbx-20261115", True),
    ("mcp-auth-drift-mcp-r1-transition", True),
    ("mcp-token-drift-specialist-drift-mcp", True),
    ("mcp-token-feature-rag-specialist-feature-rag-mcp-r1-release1", True),
    ("mcp-auth-drift-mcp-fin-20261117", False),
    ("mcp-auth-drift-mcp-fin-20261117-b", False),
    ("mcp-token-sandbox-drift-drift-mcp-sbx-20261115", False),
    ("mcp-token-kagent-controller-drift-mcp-r1-release1", False),
    ("mcp-token-revoked-drift-mcp-sbx-20261115", False),
])
def test_guard_classifies_secret_names(name, retired):
    assert (rc.classify(name) is not None) == retired


def test_guard_matches_whole_tokens_only_and_refuses_empty_input(tmp_path, capsys):
    text = "name: mcp-auth-drift-mcp-fin-20261117\nx: mcp-auth-drift-mcp\n"
    assert [f.name for f in rc.findings(text)] == ["mcp-auth-drift-mcp"]
    assert rc.findings("name: mcp-auth-drift-mcp-fin-20261117-b") == []
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert rc.main(["check", str(empty)]) == 1


def test_the_chart_retires_the_in_release_server_and_only_its_exclusive_objects(tmp_path):
    """Render the chart with a minimal in-release drift-mcp, then with retired: true."""

    base = ROOT / "infra/helm/llm-agent-platform"
    values = tmp_path / "v.yaml"
    values.write_text(json.dumps({
        "mcpServices": {"enabled": True,
                        "image": "r.example/x/mcp@sha256:" + "a" * 64,
                        "identity": {"gitSha": "b" * 40, "imageDigest": "sha256:" + "a" * 64, "configRevision": "c",
                                     "promptRevision": "p", "modelRevision": "m", "featureRevision": "f", "corpusRevision": "r"},
                        "services": {"feature-rag-mcp": {"enabled": False},
                                     "drift-mcp": {"toolGrants": {"sandbox-drift": ["drift_detection"]},
                                                   "authSecret": {"name": "mcp-auth-drift-mcp", "key": "auth-config.json"}}}},
        "mcpCredentialLifecycle": {"serverSet": "fin-20261117-b"},
        "otelCollector": {"enabled": True},
    }))
    retired = tmp_path / "r.yaml"
    retired.write_text(json.dumps({"mcpServices": {"services": {"drift-mcp": {"retired": True, "enabled": False}}}}))

    def objects(*files):
        args = ["helm", "template", "llm-agent", str(base), "-n", "default"]
        for f in files:
            args += ["-f", str(f)]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
        names = set()
        for doc in out.split("\n---"):
            kind = next((l.split(":", 1)[1].strip() for l in doc.splitlines() if l.startswith("kind:")), None)
            meta = doc.split("metadata:", 1)[-1]
            name = next((l.split(":", 1)[1].strip() for l in meta.splitlines() if l.strip().startswith("name:")), None)
            if kind:
                names.add(f"{kind}/{name}")
        return names

    try:
        before, after = objects(values), objects(values, retired)
    except FileNotFoundError:
        pytest.skip("helm is not installed")
    gone = sorted(before - after)
    assert "Deployment/drift-mcp" in gone and "ServiceAccount/drift-mcp" in gone
    assert all("drift-mcp" in item for item in gone), gone
    assert after - before == set()
    bad = tmp_path / "bad.yaml"
    bad.write_text(json.dumps({"mcpServices": {"services": {"drift-mcp": {"retired": True, "enabled": True}}}}))
    proc = subprocess.run(["helm", "template", "x", str(base), "-f", str(values), "-f", str(bad)], capture_output=True, text=True)
    assert proc.returncode != 0 and "a retired server cannot be enabled" in proc.stderr


# ------------------------------------------------------------------ KEEP-object fingerprints

from ops.llm_agent import object_fingerprints as fp  # noqa: E402


def test_fingerprints_ignore_scaling_and_cluster_bookkeeping_but_not_configuration():
    dep = {"kind": "Deployment", "metadata": {"name": "d", "resourceVersion": "1", "annotations": {"deployment.kubernetes.io/revision": "3"}},
           "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": "a"}]}}}, "status": {"readyReplicas": 2}}
    scaled = copy.deepcopy(dep)
    scaled["spec"]["replicas"] = 3
    scaled["metadata"].update(resourceVersion="9", annotations={"deployment.kubernetes.io/revision": "4"})
    scaled["status"] = {"readyReplicas": 3}
    assert fp.fingerprint(dep) == fp.fingerprint(scaled)
    image = copy.deepcopy(dep)
    image["spec"]["template"]["spec"]["containers"][0]["image"] = "b"
    assert fp.fingerprint(dep) != fp.fingerprint(image)


def test_secret_values_are_only_digested():
    secret = {"kind": "Secret", "metadata": {"name": "s"}, "data": {"token": base64.b64encode(b"VALUE-123").decode()}}
    assert "VALUE" not in json.dumps(fp.normalize(secret)) and base64.b64encode(b"VALUE-123").decode() not in json.dumps(fp.normalize(secret))
    other = copy.deepcopy(secret)
    other["data"]["token"] = "eA=="
    assert fp.fingerprint(secret) != fp.fingerprint(other)


def test_compare_requires_exactly_the_reviewed_differences():
    before = {"objects": {"Deployment/a/x": "1", "Secret/a/old": "2", "ConfigMap/a/flows": "3", "Service/a/keep": "4"}, "unserved": []}
    after = {"objects": {"Deployment/a/x": "1", "ConfigMap/a/flows": "9", "Service/a/keep": "4"}, "unserved": []}
    ok = fp.compare(before, after, ["Secret/a/old"], ["ConfigMap/a/flows"])
    assert ok["verdict"] == "PASS" and ok["unchanged_objects"] == 2
    assert fp.compare(before, after, [], ["ConfigMap/a/flows"])["verdict"] == "FAIL", "unexpected removal"
    assert fp.compare(before, after, ["Secret/a/old", "Service/a/keep"], ["ConfigMap/a/flows"])["verdict"] == "FAIL"
    drift = {"objects": dict(after["objects"], **{"Service/a/keep": "5"}), "unserved": []}
    assert fp.compare(before, drift, ["Secret/a/old"], ["ConfigMap/a/flows"])["verdict"] == "FAIL", "a KEEP object changed"
