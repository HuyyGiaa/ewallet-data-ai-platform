"""Build and compare functionally identical publisher image variants."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def command(args: list[str]) -> str:
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr}"
        )
    return result.stdout.strip()


def inspect_image(tag: str) -> dict[str, object]:
    value = json.loads(command(["docker", "image", "inspect", tag]))[0]
    history = command([
        "docker", "image", "history", "--no-trunc", "--format",
        "{{json .}}", tag,
    ]).splitlines()
    return {
        "id": value["Id"],
        "repo_digests": value.get("RepoDigests", []),
        "size_bytes": value["Size"],
        "layer_count": len(value["RootFS"]["Layers"]),
        "history": [json.loads(line) for line in history if line],
    }


def smoke(tag: str) -> dict[str, object]:
    output = command(["docker", "run", "--rm", "--network", "none", tag])
    payload = json.loads(output)
    return {
        "sha256": hashlib.sha256(output.encode()).hexdigest(),
        "flow_count": payload["flow_count"],
        "job_count": payload["job_count"],
        "dataset_count": payload["dataset_count"],
        "runs_as": command([
            "docker", "run", "--rm", "--network", "none", "--entrypoint",
            "id", tag, "-u",
        ]),
    }


def compressed_size(tag: str, destination: Path) -> int:
    safe_name = tag.replace("/", "-").replace(":", "-")
    archive = destination / f"{safe_name}.tar"
    command(["docker", "image", "save", "--output", str(archive), tag])
    import gzip
    compressed = archive.with_suffix(".tar.gz")
    with archive.open("rb") as source, gzip.open(compressed, "wb", compresslevel=6) as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    return compressed.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", type=Path, default=Path.cwd())
    parser.add_argument("--minimum-free-gib", type=float, default=20.0)
    args = parser.parse_args(argv)
    free = shutil.disk_usage(args.context).free
    if free < args.minimum_free_gib * 1024**3:
        raise RuntimeError(f"disk guard failed: only {free / 1024**3:.2f} GiB free")
    dockerfile = args.context / "images/datahub-publisher/Dockerfile"
    tags = {
        "baseline": "edai1/datahub-lineage-publisher:baseline-20261029",
        "optimized": "edai1/datahub-lineage-publisher:optimized-20261029",
    }
    started = datetime.now(timezone.utc).isoformat()
    for target, tag in tags.items():
        command([
            "docker", "build", "--pull", "--target", target,
            "--file", str(dockerfile), "--tag", tag, str(args.context),
        ])
    with tempfile.TemporaryDirectory(prefix="edai1-docker-ab-") as tmp:
        tmp_path = Path(tmp)
        variants = {}
        for name, tag in tags.items():
            variants[name] = inspect_image(tag)
            variants[name]["tag"] = tag
            variants[name]["smoke"] = smoke(tag)
            variants[name]["compressed_tar_gzip_bytes"] = compressed_size(tag, tmp_path)
    equivalent = (
        variants["baseline"]["smoke"]["sha256"]
        == variants["optimized"]["smoke"]["sha256"]
    )
    before = variants["baseline"]["size_bytes"]
    after = variants["optimized"]["size_bytes"]
    payload = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local Docker build; existing containers were not modified",
        "functional_equivalence": equivalent,
        "variants": variants,
        "uncompressed_reduction_percent": round((before - after) / before * 100, 2),
        "disk_free_bytes_before": free,
        "images_intentionally_retained_for_review": list(tags.values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "functional_equivalence": equivalent,
        "baseline_bytes": before,
        "optimized_bytes": after,
        "reduction_percent": payload["uncompressed_reduction_percent"],
    }, indent=2))
    return 0 if equivalent else 1


if __name__ == "__main__":
    raise SystemExit(main())
