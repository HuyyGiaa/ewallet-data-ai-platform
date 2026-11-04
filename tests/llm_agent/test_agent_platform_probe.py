"""Fail-closed contracts for the read-only kagent registry evidence probe."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.llm_agent.agent_platform_probe import ProbeError, build_report


def resource_list(*items: dict) -> dict:
    return {"apiVersion": "v1", "kind": "List", "items": list(items)}


def agent(name: str, uid: str, *, ready: str = "True") -> dict:
    return {
        "apiVersion": "kagent.dev/v1alpha2",
        "kind": "Agent",
        "metadata": {"name": name, "namespace": "ewallet-agents", "uid": uid},
        "spec": {"version": "1.0.0", "type": "BYO"},
        "status": {"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "Ready", "status": ready},
        ]},
    }


def owned(kind: str, name: str, uid: str) -> dict:
    return {
        "apiVersion": "apps/v1" if kind == "Deployment" else "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "ewallet-agents",
            "ownerReferences": [{
                "apiVersion": "kagent.dev/v1alpha2",
                "kind": "Agent",
                "name": name,
                "uid": uid,
                "controller": True,
            }],
        },
        "status": ({"availableReplicas": 2, "readyReplicas": 2}
                   if kind == "Deployment" else {}),
    }


class AgentPlatformProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = {
            "agents": {
                "fraud-coordinator": {
                    "version": "1.0.0",
                    "requiredSkills": ["investigate-transaction"],
                },
                "feature-rag-specialist": {
                    "version": "1.0.0",
                    "requiredSkills": ["feature-rag-evidence"],
                },
            },
            "remoteMCPServers": ["feature-rag-mcp"],
        }
        agents = [agent("fraud-coordinator", "uid-c"), agent("feature-rag-specialist", "uid-s")]
        self.capture = {
            "agents": resource_list(*agents),
            "deployments": resource_list(*[owned("Deployment", x["metadata"]["name"], x["metadata"]["uid"]) for x in agents]),
            "services": resource_list(*[owned("Service", x["metadata"]["name"], x["metadata"]["uid"]) for x in agents]),
            "remote_mcp_servers": resource_list({
                "kind": "RemoteMCPServer",
                "metadata": {"name": "feature-rag-mcp", "namespace": "ewallet-agents"},
                "status": {"conditions": [{"type": "Accepted", "status": "True"}]},
            }),
            "cards": {
                "fraud-coordinator": {
                    "name": "fraud-coordinator", "version": "1.0.0",
                    "skills": [{"id": "investigate-transaction"}],
                },
                "feature-rag-specialist": {
                    "name": "feature-rag-specialist", "version": "1.0.0",
                    "skills": [{"id": "feature-rag-evidence"}],
                },
            },
        }

    def test_complete_inventory_reports_only_sanitized_identity_and_health(self) -> None:
        dirty = copy.deepcopy(self.capture)
        dirty["agents"]["items"][0]["spec"]["token"] = "must-not-leak"
        report = build_report(self.contract, dirty)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["counts"], {"agents": 2, "remoteMCPServers": 1})
        self.assertEqual(report["agents"][0]["replicas"], {"available": 2, "ready": 2})
        self.assertNotIn("must-not-leak", json.dumps(report, sort_keys=True))

    def test_missing_readiness_fails_closed(self) -> None:
        broken = copy.deepcopy(self.capture)
        broken["agents"]["items"][0]["status"]["conditions"][1]["status"] = "False"
        with self.assertRaisesRegex(ProbeError, "fraud-coordinator.*Ready=True"):
            build_report(self.contract, broken)

    def test_mismatched_controller_ownership_fails_closed(self) -> None:
        broken = copy.deepcopy(self.capture)
        broken["deployments"]["items"][1]["metadata"]["ownerReferences"][0]["uid"] = "wrong"
        with self.assertRaisesRegex(ProbeError, "feature-rag-specialist.*Deployment.*owner"):
            build_report(self.contract, broken)

    def test_agent_card_capability_mismatch_fails_closed(self) -> None:
        broken = copy.deepcopy(self.capture)
        broken["cards"]["feature-rag-specialist"]["skills"] = []
        with self.assertRaisesRegex(ProbeError, "feature-rag-specialist.*feature-rag-evidence"):
            build_report(self.contract, broken)

    def test_cli_input_shape_can_be_reproduced_from_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, payload in self.capture.items():
                (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
            (root / "contract.json").write_text(json.dumps(self.contract), encoding="utf-8")
            self.assertEqual(json.loads((root / "agents.json").read_text())["kind"], "List")


if __name__ == "__main__":
    unittest.main()
