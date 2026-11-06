"""GPU sampling, arm rendering, traffic summaries and stage boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from ops.experiments.adapters import run_from_summary
from ops.experiments.gpu_sampler import attach, parse, sample, summarise
from ops.experiments.llm_ab_traffic import summarise as traffic_summary
from scripts.experiments import render_bench

DIGEST = "@sha256:" + "a" * 64
ARGS = ["--vllm-image", f"r/i{DIGEST}", "--gateway-image", f"r/g{DIGEST}", "--run-id", "r",
        "--api-server-cidr", "192.0.2.1/32", "--api-server-port", "443"]


def rendered(arm, capsys):
    assert render_bench.main(["exp-v3a", arm, *ARGS]) == 0
    return capsys.readouterr().out


def test_arms_differ_only_in_the_model_path(capsys):
    baseline = rendered("baseline", capsys).splitlines()
    optimized = rendered("optimized", capsys).splitlines()
    assert len(baseline) == len(optimized)
    changed = [(a, b) for a, b in zip(baseline, optimized) if a != b]
    assert len(changed) == 1
    assert "Qwen3-0.6B" in changed[0][0] and "Qwen3-1.7B" in changed[0][1]


def test_gpu_arm_requests_one_gpu_and_drops_cpu_tuning(capsys):
    text = rendered("optimized", capsys)
    assert "nvidia.com/gpu: 1" in text
    assert "--gpu-memory-utilization" in text
    assert "VLLM_CPU_OMP_THREADS_BIND" not in text
    assert "ewallet.io/bench, operator: Equal" not in text


def test_cpu_experiments_still_render_unchanged(capsys):
    assert render_bench.main(["exp-v1", "baseline", *ARGS]) == 0
    assert "nvidia.com/gpu" not in capsys.readouterr().out


def test_nvidia_smi_output_is_parsed():
    assert parse("7000, 42\n") == {"memory_mib": 7000.0, "utilization_percent": 42.0}


def test_two_gpus_are_rejected_and_recorded_as_a_gap():
    row = sample(lambda: "1, 2\n3, 4\n")
    assert row["collect_error"] == "ValueError"


def test_gap_counts_are_reported():
    rows = [{"memory_mib": 10, "utilization_percent": 50}, {"memory_mib": 20, "utilization_percent": 70},
            {"collect_error": "ValueError"}]
    assert summarise(rows) == {"samples": 2, "failed_samples": 1, "memory_mib_peak": 20, "utilization_percent_mean": 60.0}


def test_no_samples_gives_no_number():
    assert summarise([{"collect_error": "OSError"}])["memory_mib_peak"] is None


def test_gpu_figures_reach_the_comparator_paths(tmp_path):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"levels": {"1": {"requests": 2, "succeeded": 2}}}))
    samples = tmp_path / "gpu.jsonl"
    samples.write_text(json.dumps({"memory_mib": 9000, "utilization_percent": 30}) + "\n")
    attach(summary, samples)
    run = run_from_summary(json.loads(summary.read_text()), {})
    level = run.levels["1"]
    assert level["gpu_memory_mib_peak"] == 9000
    assert level["gpu_utilization_mean"] == 30


def test_unexpected_backend_and_failure_are_surfaced():
    rows = [{"status": 200, "backend": "primary", "served_model": "m", "latency_seconds": 1.0},
            {"status": 200, "backend": "other", "served_model": "x", "latency_seconds": 1.0},
            {"status": 429, "backend": "", "served_model": "", "latency_seconds": 0.1}]
    summary = traffic_summary(rows, {"primary": 100})
    assert summary["failed"] == 1
    assert summary["unexpected_backends"] == ["other"]


SCRIPT = "scripts/experiments/llm_ab.sh"


@pytest.mark.parametrize(
    ("stage", "gate"),
    [("create-gpu-pool", "GPU_POOL_APPROVED"), ("run-v3a", "EXPERIMENT_APPROVED"),
     ("run-v3b", "EXPERIMENT_APPROVED")],
)
def test_every_billable_stage_has_its_own_gate(stage, gate):
    env = {key: value for key, value in os.environ.items() if not key.endswith("_APPROVED")}
    result = subprocess.run([SCRIPT, stage, "/tmp/unused"], capture_output=True, text=True, env=env)
    assert result.returncode == 1
    assert gate in result.stderr


def test_one_approval_does_not_open_another_stage():
    env = {**os.environ, "GPU_POOL_APPROVED": "yes"}
    env.pop("EXPERIMENT_APPROVED", None)
    result = subprocess.run([SCRIPT, "run-v3a", "/tmp/unused"], capture_output=True, text=True, env=env)
    assert "EXPERIMENT_APPROVED" in result.stderr


def test_no_stage_launches_a_billable_action_without_context():
    env = {**os.environ, "GPU_POOL_APPROVED": "yes", "KUBE_CONTEXT": "kind-local"}
    result = subprocess.run([SCRIPT, "create-gpu-pool"], capture_output=True, text=True, env=env)
    assert result.returncode == 1
    assert "KUBE_CONTEXT" in result.stderr
    assert Path(SCRIPT).exists()
