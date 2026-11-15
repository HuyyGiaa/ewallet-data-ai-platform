"""helm_object_diff --expect-annotation-only: a checksum-only roll is accepted, nothing more."""

from __future__ import annotations

from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/llm_agent"))
import helm_object_diff as hod  # noqa: E402


def deployment(checksum: str, image: str = "img@sha256:a") -> dict:
    return {"kind": "Deployment", "metadata": {"name": "prometheus", "namespace": "ewallet-agents"},
            "spec": {"template": {"metadata": {"annotations": {"checksum/observability-config": checksum}},
                                  "spec": {"containers": [{"name": "p", "image": image}]}}}}


def run(tmp_path, live: dict, new: dict, *extra: str) -> int:
    (tmp_path / "live.yaml").write_text(yaml.safe_dump(live))
    (tmp_path / "new.yaml").write_text(yaml.safe_dump(new))
    return hod.main([str(tmp_path / "live.yaml"), str(tmp_path / "new.yaml"), "--expect-identities", *extra])


ARG = "--expect-annotation-only=Deployment/ewallet-agents/prometheus=checksum/observability-config"


def test_checksum_only_change_passes(tmp_path):
    assert run(tmp_path, deployment("a"), deployment("b"), ARG) == 0


def test_any_other_change_fails(tmp_path):
    assert run(tmp_path, deployment("a"), deployment("b", image="img@sha256:b"), ARG) == 1


def test_an_unchanged_object_fails(tmp_path):
    assert run(tmp_path, deployment("a"), deployment("a"), ARG) == 1


def test_without_the_expectation_the_checksum_change_is_unexpected(tmp_path):
    assert run(tmp_path, deployment("a"), deployment("b")) == 1
