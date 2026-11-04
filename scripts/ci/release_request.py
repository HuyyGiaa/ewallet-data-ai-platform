"""Write the release request the shared Jenkins pipeline hands to release_policy.

It replaces an inline heredoc in jenkins/pipeline/componentPipeline.groovy that
had never executed until edai2-cd-rag-pipeline #5 (it passed a str to
load_catalog). Components come from the change-detection scope, restricted to
the job profile; approval, branch and SHA come from the Jenkins gate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.ci.catalog import load_catalog


def build_request(*, scope: dict, catalog_path: Path, profile: str, approved_by: str, branch: str,
                  git_sha: str, environment: str, kube_context: str, image_manifest: str,
                  evidence_dir: str) -> dict:
    catalog = load_catalog(catalog_path)
    selected = list(scope["components"])
    if profile != "platform":
        selected = [name for name in selected if name in catalog.profiles[profile]]
    return {
        "requested": True,
        "owner_approved": bool(approved_by.strip()),
        "owner_approval": approved_by,
        "branch": branch,
        "git_sha": git_sha,
        "checkout_sha": git_sha,
        "environment": environment,
        "kube_context": kube_context,
        "components": selected,
        "image_manifest_path": image_manifest,
        "evidence_dir": evidence_dir,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--kube-context", required=True)
    parser.add_argument("--image-manifest", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    request = build_request(
        scope=json.loads(args.scope.read_text(encoding="utf-8")), catalog_path=args.catalog,
        profile=args.profile, approved_by=args.approved_by, branch=args.branch, git_sha=args.git_sha,
        environment=args.environment, kube_context=args.kube_context, image_manifest=args.image_manifest,
        evidence_dir=args.evidence_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
