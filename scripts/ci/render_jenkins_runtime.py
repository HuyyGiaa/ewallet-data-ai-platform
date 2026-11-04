"""Render the bounded Jenkins manifest with immutable images and proof SHAs."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
GSA = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")


def render(template: str, values: dict[str, str]) -> str:
    required = {
        "__JENKINS_CONTROLLER_IMAGE__": IMAGE,
        "__JENKINS_SOURCE_IMAGE__": IMAGE,
        "__SOURCE_SHA__": SHA,
        "__BASE_SHA__": SHA,
        "__ML_SHA__": SHA,
        "__AGENT_SHA__": SHA,
        "__DOCS_SHA__": SHA,
        "__JENKINS_GSA__": GSA,
    }
    for token, pattern in required.items():
        value = values[token]
        if not pattern.fullmatch(value):
            raise ValueError(f"invalid immutable Jenkins runtime value for {token}")
        if template.count(token) < 1:
            raise ValueError(f"missing Jenkins runtime token {token}")
        template = template.replace(token, value)
    if re.search(r"__[A-Z0-9_]+__", template):
        raise ValueError("unresolved Jenkins runtime template token")
    return template


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=Path("infra/kubernetes/jenkins/jenkins.yaml"))
    parser.add_argument("--controller-image", required=True)
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--ml-sha", required=True)
    parser.add_argument("--agent-sha", required=True)
    parser.add_argument("--docs-sha", required=True)
    parser.add_argument("--jenkins-gsa", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    content = render(args.template.read_text(encoding="utf-8"), {
        "__JENKINS_CONTROLLER_IMAGE__": args.controller_image,
        "__JENKINS_SOURCE_IMAGE__": args.source_image,
        "__SOURCE_SHA__": args.source_sha,
        "__BASE_SHA__": args.base_sha,
        "__ML_SHA__": args.ml_sha,
        "__AGENT_SHA__": args.agent_sha,
        "__DOCS_SHA__": args.docs_sha,
        "__JENKINS_GSA__": args.jenkins_gsa,
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
