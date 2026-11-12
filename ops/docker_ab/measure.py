"""Measure one built variant: image metadata, functional run, user and hardening."""

from __future__ import annotations

import json
import statistics
from typing import Any, Mapping, Sequence

from .docker_cli import DockerCli, Mount
from .variants import Variant

CONTAINER_EVIDENCE = "/input/evidence"
CONTAINER_SPEC = "/input/workload.json"
VERIFIER_ARGUMENTS = ("--spec", CONTAINER_SPEC, "--evidence-dir", CONTAINER_EVIDENCE)


def image_metadata(cli: DockerCli, tag: str, base_reference: str) -> dict[str, Any]:
    """Size, layer count and payload of one image relative to its base image."""

    image = cli.inspect(tag)
    base = cli.inspect(base_reference)
    layers = list(image["RootFS"]["Layers"])
    base_layers = list(base["RootFS"]["Layers"])
    if layers[: len(base_layers)] != base_layers:
        raise ValueError(f"{tag} does not start with the layers of its base image")
    return {
        "image_id": image["Id"],
        # With the containerd image store, Size is the compressed content (pull) size.
        "content_size_bytes": int(image["Size"]),
        "disk_usage_reported": cli.disk_usage(tag),
        "layer_count": len(layers),
        "base_image": base_reference,
        "base_image_id": base["Id"],
        "base_repo_digests": sorted(base.get("RepoDigests") or []),
        "base_content_size_bytes": int(base["Size"]),
        "added_by_dockerfile_bytes": int(image["Size"]) - int(base["Size"]),
        "added_layer_count": len(layers) - len(base_layers),
        "config_user": image["Config"].get("User") or "",
        "entrypoint": image["Config"].get("Entrypoint"),
        "architecture": image.get("Architecture"),
        "history": [
            {"created_by": item.get("CreatedBy", ""), "size": item.get("Size", "")}
            for item in cli.history(tag)
        ],
    }


def functional_run(cli: DockerCli, tag: str, mounts: Sequence[Mount], repetitions: int) -> dict[str, Any]:
    """Run the verifier ``repetitions`` times; every run must PASS with one fingerprint."""

    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    runs = []
    for _ in range(repetitions):
        code, stdout, stderr, seconds = cli.run(tag, VERIFIER_ARGUMENTS, mounts)
        lines = [line for line in stdout.splitlines() if line.strip()]
        summary = json.loads(lines[-1]) if lines else {}
        runs.append({"exit_code": code, "seconds": round(seconds, 4), "summary": summary,
                     "stderr_tail": stderr.strip()[-300:]})
    fingerprints = sorted({str(run["summary"].get("report_sha256")) for run in runs})
    seconds = [run["seconds"] for run in runs]
    return {
        "runs": runs,
        "all_passed": all(run["exit_code"] == 0 and run["summary"].get("verdict") == "PASS" for run in runs),
        "report_sha256": fingerprints[0] if len(fingerprints) == 1 else None,
        "deterministic": len(fingerprints) == 1,
        "run_seconds": {
            "median": round(statistics.median(seconds), 4),
            "minimum": min(seconds),
            "maximum": max(seconds),
            "samples": seconds,
        },
    }


def security_properties(cli: DockerCli, tag: str) -> dict[str, Any]:
    """Runtime identity and what the image ships beyond the application."""

    tools = cli.probe(tag, ["sh", "-c", "for t in gcc make git curl apt-get; do "
                                        "command -v $t >/dev/null 2>&1 && echo $t; done"])
    return {
        "runtime_uid_gid": cli.probe(tag, ["id", "-u"]) + ":" + cli.probe(tag, ["id", "-g"]),
        "runs_as_root": cli.probe(tag, ["id", "-u"]) == "0",
        "shipped_tools": sorted(line for line in tools.splitlines() if line),
        "application_files_writable_by_runtime_user": cli.probe(
            tag, ["sh", "-c", "find /app -writable -type f | head -1"]) != "",
    }


def attribution(sizes: Mapping[str, int]) -> dict[str, Any]:
    """Split (content-size bytes) the baseline -> optimized reduction into base-image and packaging effects.

    Each effect is measured twice (once per level of the other factor); the two
    estimates and their interaction are reported rather than a single number.
    """

    full_naive, slim_naive = sizes["baseline"], sizes["baseline-slim"]
    full_minimal, slim_minimal = sizes["optimized-full"], sizes["optimized"]
    total = full_naive - slim_minimal

    def share(value: int) -> float | None:
        return round(value / total * 100, 2) if total else None

    base_naive = full_naive - slim_naive
    base_minimal = full_minimal - slim_minimal
    payload_full = full_naive - full_minimal
    payload_slim = slim_naive - slim_minimal
    return {
        "total_reduction_bytes": total,
        "total_reduction_percent": round(total / full_naive * 100, 2) if full_naive else None,
        "base_image_effect_bytes": {"with_full_context_packaging": base_naive, "with_minimal_payload": base_minimal},
        "packaging_effect_bytes": {"on_full_base": payload_full, "on_slim_base": payload_slim},
        "share_of_total_percent": {
            "base_image_then_packaging": {"base_image": share(base_naive), "packaging": share(payload_slim)},
            "packaging_then_base_image": {"packaging": share(payload_full), "base_image": share(base_minimal)},
        },
        "interaction_bytes": base_naive - base_minimal,
    }
