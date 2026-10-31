"""Validate immutable F9 image evidence against a compiled KFP package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

import yaml


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_IMAGE = re.compile(r"^.+@sha256:([0-9a-f]{64})$")


def _images(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "image" and isinstance(item, str):
                found.add(item)
            found.update(_images(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_images(item))
    return found


def validate_f9_artifacts(
    evidence: Mapping[str, object],
    compiled_yaml: bytes,
    *,
    expected_source_git_sha: str,
) -> None:
    if not _GIT_SHA.fullmatch(expected_source_git_sha):
        raise ValueError("expected source Git SHA is invalid")
    if evidence.get("schema_version") != "f9-immutable-artifact-evidence-v1":
        raise ValueError("F9 artifact evidence schema is invalid")
    if evidence.get("status") != "BUILT_AND_DIGEST_VERIFIED":
        raise ValueError("F9 image has not been built and digest-verified")
    if evidence.get("source_git_sha") != expected_source_git_sha:
        raise ValueError("F9 image source Git SHA is stale or mismatched")
    image = evidence.get("image_uri")
    match = _DIGEST_IMAGE.fullmatch(str(image)) if isinstance(image, str) else None
    if match is None or evidence.get("image_digest") != f"sha256:{match.group(1)}":
        raise ValueError("F9 image must be bound by one immutable digest")
    actual_yaml_sha = hashlib.sha256(compiled_yaml).hexdigest()
    if evidence.get("compiled_yaml_sha256") != actual_yaml_sha:
        raise ValueError("compiled KFP YAML checksum mismatch")
    document = yaml.safe_load(compiled_yaml)
    images = _images(document)
    if not images or images != {image}:
        raise ValueError("compiled KFP YAML contains stale or mismatched images")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--compiled-yaml", type=Path, required=True)
    parser.add_argument("--expected-source-git-sha", required=True)
    args = parser.parse_args(argv)
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    validate_f9_artifacts(
        evidence,
        args.compiled_yaml.read_bytes(),
        expected_source_git_sha=args.expected_source_git_sha,
    )
    print("PASS | immutable F9 image and compiled KFP YAML are compatible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
