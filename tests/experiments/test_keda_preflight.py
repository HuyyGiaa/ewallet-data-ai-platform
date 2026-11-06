"""Pure logic of the read-only KEDA preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.experiments import keda_preflight as k

ROOT = Path(__file__).resolve().parents[2]


def node(name: str, cpu: str = "4", memory: str = "8Gi", label: bool = True) -> dict:
    labels = {k.LLM_NODE_LABEL: "true"} if label else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"allocatable": {"cpu": cpu, "memory": memory}}}


def pod(node_name: str, cpu: str, memory: str, phase: str = "Running") -> dict:
    return {"spec": {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]},
            "status": {"phase": phase}}


def deployment(name: str, replicas: int) -> dict:
    return {"metadata": {"name": name}, "spec": {"replicas": replicas, "template": {"spec": {"containers": [
        {"resources": {"requests": {"cpu": "100m", "memory": "256Mi"}}}]}}}}


@pytest.mark.parametrize(("value", "cores"), [("100m", 0.1), ("2", 2.0), ("3920m", 3.92)])
def test_cpu_quantities(value, cores) -> None:
    assert k.cpu(value) == pytest.approx(cores)


@pytest.mark.parametrize(("value", "mib"), [("256Mi", 256), ("1Gi", 1024), ("2048Ki", 2), (str(1048576), 1), ("32M", 30.5176), ("1G", 953.674)])
def test_memory_quantities(value, mib) -> None:
    assert k.memory_mib(value) == pytest.approx(mib)


def test_free_capacity_counts_only_labelled_nodes_and_live_pods() -> None:
    nodes = {"items": [node("a"), node("b", label=False)]}
    pods = {"items": [pod("a", "1", "1Gi"), pod("a", "1", "1Gi", "Succeeded"), pod("b", "1", "1Gi")]}
    assert k.free_capacity(nodes, pods, k.LLM_NODE_LABEL) == {"a": (3.0, 7168.0)}


def test_replicas_that_fit_only_across_nodes_pass() -> None:
    assert k.worst_case_fits({"a": (0.15, 1000), "b": (0.15, 1000)}, [(0.1, 100), (0.1, 100)])


def test_one_replica_too_many_fails() -> None:
    assert not k.worst_case_fits({"a": (0.15, 1000)}, [(0.1, 100), (0.1, 100)])


def test_memory_can_block_as_well() -> None:
    assert not k.worst_case_fits({"a": (4.0, 100)}, [(0.1, 256)])


def test_capacity_counts_every_agent_at_the_maximum() -> None:
    deployments = {"items": [deployment(name, 2) for name in k.AGENTS] + [deployment("other", 1)]}
    check = k.capacity_check({"items": [node("a", "350m")]}, {"items": []}, deployments, 3)
    assert check.status == k.READY and "3 extra" in check.detail
    assert k.capacity_check({"items": [node("a", "250m")]}, {"items": []}, deployments, 3).status == k.BLOCKED


def hpa(name: str, low: int = 2, high: int = 3) -> dict:
    return {"metadata": {"name": name}, "spec": {"minReplicas": low, "maxReplicas": high}}


def test_plain_hpa_must_go_before_the_scaledobject() -> None:
    assert k.hpa_check({"items": [hpa("fraud-coordinator")]}, "before-scaledobject").status == k.WAITING
    assert k.hpa_check({"items": [hpa("fraud-coordinator")]}, "before-install").status == k.READY


def test_unexpected_replica_range_blocks() -> None:
    assert k.hpa_check({"items": [hpa("fraud-coordinator", high=9)]}, "before-install").status == k.BLOCKED


def test_kedas_own_hpa_is_not_a_plain_hpa() -> None:
    assert k.hpa_check({"items": [hpa("keda-hpa-fraud-coordinator")]}, "before-scaledobject").status == k.READY
