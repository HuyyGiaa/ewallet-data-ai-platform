"""Bake one registered MLflow model version into the KServe predictor image.

Resolves ``models:/<name>@<alias>`` (default alias ``candidate``), downloads
that exact version, writes ``model_identity.json`` and assembles a minimal
build context (Dockerfile, serving uv project, ai_platform Python modules,
model). With ``--execute`` it runs Cloud Build and records the image digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def assemble(args: argparse.Namespace) -> dict[str, object]:
    import mlflow
    from mlflow import MlflowClient

    from ai_platform.ml.tracking.mlflow_tracker import python_package_snapshot

    mlflow.set_tracking_uri(args.tracking_uri)
    client = MlflowClient()
    version = client.get_model_version_by_alias(args.model_name, args.alias)
    context = args.context_dir
    if context.exists():
        raise FileExistsError(f"build context already exists: {context}")
    model_root = context / "models" / "fraud"
    mlflow.artifacts.download_artifacts(
        artifact_uri=f"models:/{args.model_name}/{version.version}",
        dst_path=str(model_root / "model"),
    )
    identity = {
        "registered_model_name": args.model_name,
        "registered_model_version": str(version.version),
        "resolved_alias": args.alias,
        "run_id": version.run_id,
        "source": version.source,
        "tags": dict(version.tags),
        "model_tree_sha256": _tree_sha256(model_root / "model"),
        "baked_at": datetime.now(timezone.utc).isoformat(),
        "source_git_sha": args.git_sha,
    }
    (model_root / "model_identity.json").write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    serving = context / "ai_platform" / "serving"
    python_package_snapshot(context, REPO_ROOT / "ai_platform")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(REPO_ROOT / "ai_platform" / "serving" / name, serving / name)
    shutil.copy2(REPO_ROOT / "ai_platform" / "serving" / "Dockerfile", context / "Dockerfile")
    return identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--alias", default="candidate")
    parser.add_argument("--image-repository", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--project-id", default="ewallet-data-ai-platform-hg26")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.alias == "production":
        parser.error("production images are built only after the manual promotion gate")
    identity = assemble(args)
    tag = f"{args.image_repository}:git-{args.git_sha[:12]}-mv{identity['registered_model_version']}"
    evidence = {"status": "ASSEMBLED", "image_tag": tag, "identity": identity}
    if args.execute:
        subprocess.run(
            ["gcloud", "builds", "submit", str(args.context_dir), f"--project={args.project_id}",
             f"--region={args.region}", f"--tag={tag}"],
            check=True,
        )
        digest = subprocess.check_output(
            ["gcloud", "artifacts", "docker", "images", "describe", tag,
             f"--project={args.project_id}", "--format=value(image_summary.digest)"],
            text=True,
        ).strip()
        if not digest.startswith("sha256:"):
            raise RuntimeError(f"could not resolve digest for {tag}")
        evidence.update(status="BUILT", image=f"{args.image_repository}@{digest}")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
