"""Docker A/B harness, Dockerfiles and Compose service (rubric rows 3-4), without a Docker daemon."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from ops.docker_ab import IMAGE_REPOSITORY
from ops.docker_ab.docker_cli import DockerCli, Mount
from ops.docker_ab.measure import attribution, functional_run
from ops.docker_ab.report import render_text
from ops.docker_ab.variants import FULL_BASE, SLIM_BASE, VARIANTS, variant

ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIR = ROOT / "images/edai1-campaign-verifier"


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, args, **_):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


def test_the_matrix_isolates_base_image_and_packaging():
    assert [item.name for item in VARIANTS] == ["baseline", "baseline-slim", "optimized-full", "optimized"]
    assert {(item.packaging, item.base_image) for item in VARIANTS} == {
        (packaging, base) for packaging in ("single-stage-full-context", "multi-stage-minimal-payload")
        for base in (FULL_BASE, SLIM_BASE)}
    assert variant("optimized").role == "optimized" and variant("baseline").role == "baseline"
    assert variant("optimized").tag("20261112a") == f"{IMAGE_REPOSITORY}:optimized-20261112a"
    with pytest.raises(ValueError):
        variant("optimized").tag("BAD TAG")


def test_dockerfile_defaults_equal_the_harness_pins():
    baseline = (IMAGE_DIR / "Dockerfile.baseline").read_text()
    optimized = (IMAGE_DIR / "Dockerfile.optimized").read_text()
    assert f"ARG BASE_IMAGE={FULL_BASE}" in baseline
    assert f"ARG RUNTIME_IMAGE={SLIM_BASE}" in optimized and f"ARG BUILD_IMAGE={SLIM_BASE}" in optimized
    assert all("@sha256:" in line for line in (baseline + optimized).splitlines() if line.startswith("ARG "))


def test_baseline_is_single_stage_root_full_context():
    text = (IMAGE_DIR / "Dockerfile.baseline").read_text()
    assert len(re.findall(r"^FROM ", text, re.MULTILINE)) == 1
    assert "COPY . /app" in text
    assert "USER" not in text


def test_optimized_is_multi_stage_non_root_with_minimal_context():
    text = (IMAGE_DIR / "Dockerfile.optimized").read_text()
    assert re.findall(r"^FROM \S+ AS (\w+)", text, re.MULTILINE) == ["build", "runtime"]
    assert "COPY --from=build --chown=0:0 /build/edai1_verifier /app/edai1_verifier" in text
    assert "USER 65532:65532" in text
    assert "python -m compileall" in text
    ignore = (IMAGE_DIR / "Dockerfile.optimized.dockerignore").read_text().split()
    assert ignore[ignore.index("*") + 1] == "!ops/edai1_verifier/"


def test_compose_runs_the_optimized_verifier_hardened_on_read_only_inputs():
    compose = yaml.safe_load((ROOT / "infra/docker/compose.edai1.yml").read_text())
    service = compose["services"]["campaign-verifier"]
    assert service["build"]["dockerfile"] == "images/edai1-campaign-verifier/Dockerfile.optimized"
    assert service["build"]["target"] == "runtime"
    assert all(volume["read_only"] for volume in service["volumes"])
    assert {volume["target"] for volume in service["volumes"]} == {"/input/evidence", "/input/workload.json"}
    assert service["command"] == ["--spec", "/input/workload.json", "--evidence-dir", "/input/evidence"]
    assert (service["user"], service["read_only"], service["network_mode"], service["cap_drop"], service["restart"]) \
        == ("65532:65532", True, "none", ["ALL"], "no")
    for volume in service["volumes"]:
        assert (ROOT / "infra/docker" / volume["source"]).resolve().exists()


def test_build_is_uncached_with_explicit_args_and_target(tmp_path):
    runner = FakeRunner()
    cli = DockerCli(ROOT, runner)
    cli.build(variant("optimized-full"), "edai1/campaign-verifier:optimized-full-t", log=tmp_path / "log")
    args = runner.calls[0]
    assert args[:4] == ["docker", "build", "--no-cache", "--pull=false"]
    assert f"BUILD_IMAGE={FULL_BASE}" in args and f"RUNTIME_IMAGE={FULL_BASE}" in args
    assert args[args.index("--target") + 1] == "runtime"
    assert (tmp_path / "log").read_text().startswith("$ docker build")


def test_runs_are_isolated_and_read_only():
    runner = FakeRunner(stdout=json.dumps({"verdict": "PASS", "report_sha256": "x" * 64}) + "\n")
    cli = DockerCli(ROOT, runner)
    result = functional_run(cli, "tag", [Mount(ROOT / "images", "/input/evidence")], 3)
    args = runner.calls[0]
    for flag in ("--network", "--read-only", "--cap-drop"):
        assert flag in args
    assert any(arg.endswith("target=/input/evidence,readonly") for arg in args)
    assert result["all_passed"] and result["deterministic"] and len(result["runs"]) == 3


def test_a_variant_with_another_fingerprint_is_not_deterministic():
    outputs = iter([json.dumps({"verdict": "PASS", "report_sha256": value}) for value in ("a", "b")])

    def runner(args, **_):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    result = functional_run(DockerCli(ROOT, runner), "tag", [], 2)
    assert not result["deterministic"] and result["report_sha256"] is None


def test_attribution_separates_base_image_and_packaging():
    sizes = {"baseline": 396_893_122, "baseline-slim": 59_101_276, "optimized-full": 381_979_402,
             "optimized": 44_187_548}
    result = attribution(sizes)
    assert result["total_reduction_bytes"] == 352_705_574
    assert result["base_image_effect_bytes"] == {"with_full_context_packaging": 337_791_846,
                                                 "with_minimal_payload": 337_791_854}
    assert result["packaging_effect_bytes"] == {"on_full_base": 14_913_720, "on_slim_base": 14_913_728}
    assert result["interaction_bytes"] == -8


def test_summary_states_the_size_metric_and_equivalence():
    def item(name, size):
        return {"role": variant(name).role, "packaging": variant(name).packaging, "build_seconds": 1.0,
                "image": {"base_image": variant(name).base_image, "content_size_bytes": size,
                          "disk_usage_reported": "1MB", "layer_count": 7, "added_by_dockerfile_bytes": 1},
                "functional": {"run_seconds": {"median": 0.4}, "all_passed": True, "report_sha256": "f" * 64},
                "security": {"runtime_uid_gid": "65532:65532"}}

    sizes = {"baseline": 400, "baseline-slim": 60, "optimized-full": 380, "optimized": 40}
    result = {"run_id": "t", "tool_git_sha": "a" * 40,
              "docker": {"engine": "29.4.0", "architecture": "amd64"},
              "workload": {"spec_file": "s", "evidence_dir": "e", "benchmark_id": "ab-20261110225456",
                           "source_git_sha": "b" * 40},
              "variants": {name: item(name, size) for name, size in sizes.items()},
              "functional_equivalence": {"equivalent": True, "variants": 4, "report_sha256": "f" * 64},
              "attribution": attribution(sizes)}
    text = render_text(result)
    assert "functional equivalence: PASS" in text
    assert "compressed content (pull) bytes" in text
    assert text.count("PASS") >= 5
