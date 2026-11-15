"""Scheduling preflight of the isolated MCP experiment, on the real rendered arms.

The four-node fixture is the state observed on 2026-11-14 (agi-20261114t221240z): three nodes
labelled ewallet.io/g3-llm and the fourth at about 97% of its CPU requested.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

from ops.experiments import mcp_capacity as cap

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "d" * 64
pytestmark = pytest.mark.skipif(importlib.util.find_spec("yaml") is None, reason="PyYAML required")


def rendered(arm: str) -> str:
    return subprocess.run([sys.executable, str(ROOT / "scripts/experiments/render_mcp.py"), arm,
                           "--mcp-image", f"r/mcp@{DIGEST}", "--probe-image", f"r/probe@{DIGEST}",
                           "--release-sha", "a" * 40, "--duration", "120", "--rate", "2"],
                          check=True, capture_output=True, text=True, cwd=ROOT).stdout


def node(name, llm, used_cpu_m, ready=True, unschedulable=False):
    labels = {"kubernetes.io/hostname": name, **({"ewallet.io/g3-llm": "true"} if llm else {})}
    return ({"metadata": {"name": name, "labels": labels},
             "spec": {"unschedulable": unschedulable},
             "status": {"allocatable": {"cpu": "3920m", "memory": "13273Mi"},
                        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}},
            {"metadata": {"name": f"load-{name}"}, "spec": {"nodeName": name, "containers": [
                {"resources": {"requests": {"cpu": f"{used_cpu_m}m", "memory": "6000Mi"}}}]}, "status": {"phase": "Running"}})


def cluster(*specs):
    pairs = [node(*spec) for spec in specs]
    return cap.free_capacity([n for n, _ in pairs], [p for _, p in pairs])


OBSERVED_FOUR = (("0kdn", True, 3754), ("gtkf", True, 3916), ("jd6v", True, 1734), ("r761", False, 3828))
FIVE_NODES = (("a", True, 3700), ("b", True, 3800), ("c", True, 1700), ("d", False, 2100), ("e", False, 1900))


def test_requirements_come_from_the_rendered_manifest():
    base, opt = cap.requirements(rendered("baseline")), cap.requirements(rendered("optimized"))
    assert (base["replicas"], opt["replicas"]) == (1, 2)
    assert base["excluded_labels"] == opt["excluded_labels"] == ["ewallet.io/g3-llm"]
    assert opt["hostname_spread"] and opt["cpu_m"] == 100 and opt["mem_mi"] == 256
    assert (opt["probe_cpu_m"], opt["probe_mem_mi"]) == (50, 128)


@pytest.mark.parametrize("arm", ["baseline", "optimized"])
def test_the_observed_four_node_wake_is_refused_with_a_reason(arm):
    report = cap.decide(cap.requirements(rendered(arm)), cluster(*OBSERVED_FOUR))
    assert report["verdict"] == "CAPACITY_INSUFFICIENT"
    assert [n["name"] for n in report["eligible_nodes"]] == ["r761"]
    assert report["reasons"]


@pytest.mark.parametrize("arm", ["baseline", "optimized"])
def test_two_eligible_nodes_with_room_are_schedulable(arm):
    report = cap.decide(cap.requirements(rendered(arm)), cluster(*FIVE_NODES))
    assert report["verdict"] == "SCHEDULABLE", report["reasons"]


def test_optimized_refuses_both_replicas_on_one_node_even_if_they_fit():
    one_roomy = (("a", True, 1000), ("b", True, 1000), ("c", True, 1000), ("d", False, 500))
    assert cap.decide(cap.requirements(rendered("baseline")), cluster(*one_roomy))["verdict"] == "SCHEDULABLE"
    report = cap.decide(cap.requirements(rendered("optimized")), cluster(*one_roomy))
    assert report["verdict"] == "CAPACITY_INSUFFICIENT"
    assert "2 distinct eligible nodes" in report["reasons"][0]


def test_not_ready_cordoned_and_finished_pods_are_handled():
    specs = (("a", True, 1000), ("d", False, 500, False), ("e", False, 500, True, True))
    report = cap.decide(cap.requirements(rendered("baseline")), cluster(*specs))
    assert report["verdict"] == "CAPACITY_INSUFFICIENT" and report["eligible_nodes"] == []
    nodes = [node("d", False, 0)[0]]
    done = {"spec": {"nodeName": "d", "containers": [{"resources": {"requests": {"cpu": "3900m"}}}]}, "status": {"phase": "Succeeded"}}
    assert cap.free_capacity(nodes, [done])[0]["free_cpu_m"] == 3920


def test_quantities():
    assert cap.cpu_m("1") == 1000 and cap.cpu_m("250m") == 250 and cap.cpu_m(None) == 0
    assert cap.mem_mi("1Gi") == 1024 and cap.mem_mi("512Mi") == 512 and round(cap.mem_mi("1048576")) == 1


def test_run_checks_capacity_before_creating_anything():
    script = (ROOT / "scripts/experiments/mcp_experiment.sh").read_text()
    check = script.index('capacity_check "$evidence/capacity-$arm-')
    assert check < script.index("awk '/^---$/ {exit} {print}' \"$config\" | kc apply")
    assert check < script.index("trap cleanup EXIT")
    assert "CAPACITY_INSUFFICIENT" in script and "nothing was created" in script
