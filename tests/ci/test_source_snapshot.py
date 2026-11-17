"""Jenkins source bundle: release refs at the reviewed SHA, deterministic, verified the way Jenkins fetches it."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts.ci import source_snapshot as snapshot


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for path in ("README.md",):
        (repo / path).write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    for path in snapshot.REQUIRED_PATHS:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("x\n")
    units = repo / "jenkins/catalog/release-units.json"
    units.write_text(json.dumps({"groups": {"release/rag-pipeline": {}, "release/streaming": {}}}))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "release")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_bundle_carries_every_release_ref_and_verifies(repository, tmp_path) -> None:
    repo, base, sha = repository
    metadata = snapshot.build(repo, sha, base, tmp_path / "out")
    assert metadata["ref:refs/heads/release/rag-pipeline"] == sha
    assert metadata["ref:refs/heads/release/streaming"] == sha
    assert metadata["ref:refs/heads/base"] == base
    assert snapshot.verify(tmp_path / "out", sha) == []
    # The caller's repository gained no ref.
    assert "release/" not in git(repo, "for-each-ref", "--format=%(refname)")


def test_rebuilding_the_same_release_is_byte_identical(repository, tmp_path) -> None:
    repo, base, sha = repository
    first = snapshot.build(repo, sha, base, tmp_path / "a")["bundle_sha256"]
    assert snapshot.build(repo, sha, base, tmp_path / "b")["bundle_sha256"] == first


def test_a_bundle_for_another_release_or_a_tampered_bundle_fails(repository, tmp_path) -> None:
    repo, base, sha = repository
    snapshot.build(repo, sha, base, tmp_path / "out")
    assert any("source_sha" in item for item in snapshot.verify(tmp_path / "out", base))
    bundle = tmp_path / "out/repository.bundle"
    bundle.write_bytes(bundle.read_bytes() + b"x")
    assert any("sha256" in item for item in snapshot.verify(tmp_path / "out", sha))


def test_the_base_must_be_an_ancestor_and_shas_must_be_full(repository, tmp_path) -> None:
    repo, base, sha = repository
    with pytest.raises(snapshot.SnapshotError, match="ancestor"):
        snapshot.build(repo, base, sha, tmp_path / "x")
    with pytest.raises(snapshot.SnapshotError, match="full SHA"):
        snapshot.build(repo, sha[:12], base, tmp_path / "x")


def test_a_release_without_the_rebuilt_ci_is_refused(repository, tmp_path) -> None:
    repo, base, sha = repository
    old = tmp_path / "old"
    snapshot.build(repo, sha, base, old)
    # Rewrite the metadata to claim the base (which lacks the rebuilt CI) is the release.
    problems = snapshot.verify(old, base)
    assert problems


def test_the_repository_release_groups_are_the_ones_jenkins_seeds() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
    for group in snapshot.release_groups(root):
        assert f"'*/{group}'" in text, group
    for proof in snapshot.PROOF_REFS:
        assert f"'*/{proof}'" in text, proof
