"""Render the benchmark result: machine-readable JSON and a compact CLI summary."""

from __future__ import annotations

from typing import Any, Mapping

COLUMNS = ("variant", "role", "packaging", "base", "content_bytes", "content_MiB", "disk_usage", "layers", "added_bytes",
           "build_s", "run_s", "uid", "verdict", "report_sha256")


def _base_label(reference: str) -> str:
    return reference.split("@", 1)[0]


def summary_rows(result: Mapping[str, Any]) -> list[dict[str, str]]:
    """One row per variant, ordered as built."""

    rows = []
    for name, item in result["variants"].items():
        image = item["image"]
        run = item["functional"]
        rows.append({
            "variant": name,
            "role": item["role"],
            "packaging": item["packaging"],
            "base": _base_label(image["base_image"]),
            "content_bytes": f"{image['content_size_bytes']:,}",
            "content_MiB": f"{image['content_size_bytes'] / 2**20:.1f}",
            "disk_usage": image["disk_usage_reported"] or "-",
            "layers": str(image["layer_count"]),
            "added_bytes": f"{image['added_by_dockerfile_bytes']:,}",
            "build_s": f"{item['build_seconds']:.1f}",
            "run_s": f"{run['run_seconds']['median']:.2f}",
            "uid": item["security"]["runtime_uid_gid"],
            "verdict": "PASS" if run["all_passed"] else "FAIL",
            "report_sha256": (run["report_sha256"] or "NOT_DETERMINISTIC")[:16],
        })
    return rows


def render_text(result: Mapping[str, Any]) -> str:
    """Fixed-width table plus the equivalence and attribution lines (DK03/DK04)."""

    rows = summary_rows(result)
    widths = {column: max(len(column), *(len(row[column]) for row in rows)) for column in COLUMNS}
    lines = [
        f"EDAI-1 Docker A/B  run={result['run_id']}  tool_git_sha={result['tool_git_sha'][:12]}  "
        f"docker={result['docker']['engine']}  arch={result['docker']['architecture']}",
        f"workload: {result['workload']['spec_file']} on {result['workload']['evidence_dir']} "
        f"(campaign {result['workload']['benchmark_id']}, source Spark {result['workload']['source_git_sha'][:12]})",
        "  ".join(column.ljust(widths[column]) for column in COLUMNS),
        "  ".join("-" * widths[column] for column in COLUMNS),
    ]
    lines += ["  ".join(row[column].ljust(widths[column]) for column in COLUMNS) for row in rows]
    equivalence = result["functional_equivalence"]
    attribution = result["attribution"]
    lines += [
        "",
        f"functional equivalence: {'PASS' if equivalence['equivalent'] else 'FAIL'} "
        f"(one report SHA-256 across {equivalence['variants']} variants: {equivalence['report_sha256']})",
        "sizes are compressed content (pull) bytes from docker image inspect; disk_usage is the unpacked size "
        "reported by docker image ls",
        f"baseline -> optimized: {attribution['total_reduction_bytes']:,} content bytes "
        f"({attribution['total_reduction_percent']} %)",
        f"  base image effect: {attribution['base_image_effect_bytes']}",
        f"  packaging effect:  {attribution['packaging_effect_bytes']}",
    ]
    compose = result.get("compose")
    if compose:
        lines.append(f"compose: config={compose['config_valid']} build_exit={compose['build_exit_code']} "
                     f"run_exit={compose['run_exit_code']} verdict={compose['verdict']} "
                     f"residual_containers={compose['residual_containers']}")
    return "\n".join(lines) + "\n"
