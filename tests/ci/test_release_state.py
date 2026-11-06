"""Promotion must change only images, and Helm must stay the record of what runs."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from scripts.ci import release_state as rs
from tests.llm_agent.test_agents_chart import HELM, ON, render
from tests.llm_agent.test_n11_chart import BYO

REGISTRY = "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform"
OLD = f"{REGISTRY}/mcp-tools@sha256:" + "a" * 64
NEW = f"{REGISTRY}/mcp-tools@sha256:" + "b" * 64
SHA = "c" * 40
KEY = ("Deployment", "ewallet-mcp", "feature-rag-mcp")


def deployment(image: str = OLD, replicas: int = 1) -> dict:
    return {"kind": "Deployment", "metadata": {"name": KEY[2], "namespace": KEY[1], "annotations": {}},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [
                {"name": "server", "image": image, "command": ["python"],
                 "env": [{"name": "EWALLET_IMAGE_DIGEST", "value": "sha256:a"}, {"name": "LOG_LEVEL", "value": "INFO"}]}]}}}}


def test_matching_images_have_no_drift() -> None:
    assert rs.drift({KEY: deployment()}, lambda key: deployment()) == []


def test_hand_patched_image_is_drift() -> None:
    assert "differs" in rs.drift({KEY: deployment()}, lambda key: deployment(NEW))[0]


def test_missing_object_is_drift() -> None:
    assert "absent" in rs.drift({KEY: deployment()}, lambda key: None)[0]


def test_objects_without_images_are_skipped() -> None:
    assert rs.drift({("Service", "n", "s"): {"kind": "Service"}}, lambda key: None) == []


def test_agent_image_is_read_from_the_byo_block() -> None:
    assert rs.declared_images({"kind": "Agent", "spec": {"byo": {"deployment": {"image": NEW}}}}) == [NEW]
    assert rs.declared_images({"kind": "Agent", "spec": {"type": "Declarative"}}) == []


def test_nothing_compared_is_an_error_not_a_pass() -> None:
    with pytest.raises(rs.ReleaseStateError, match="nothing was compared"):
        rs.check_drift(lambda command: "kind: Service\nmetadata: {name: x}\n", "llm-agent", "ctx")


def test_failed_cluster_read_is_an_error() -> None:
    def run(command):
        if command[0] == "kubectl":
            raise rs.ReleaseStateError("kubectl failed")
        return yaml.safe_dump(deployment())

    with pytest.raises(rs.ReleaseStateError):
        rs.check_drift(run, "llm-agent-mcp", "ctx")


def test_image_and_identity_changes_are_allowed() -> None:
    after = deployment(NEW)
    after["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "sha256:b"
    after["metadata"]["annotations"]["ewallet.io/release-sha"] = SHA
    assert rs.non_image_changes({KEY: deployment()}, {KEY: after}) == []


@pytest.mark.parametrize("change", [
    lambda d: d["spec"].update(replicas=5),
    lambda d: d["spec"]["template"]["spec"]["containers"][0].update(command=["sh"]),
    lambda d: d["spec"]["template"]["spec"]["containers"][0]["env"][1].update(value="DEBUG"),
    lambda d: d["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "X", "value": "1"}),
    lambda d: d["spec"]["template"]["spec"].update(serviceAccountName="other"),
    lambda d: d["metadata"]["annotations"].update(other="x"),
], ids=["replicas", "command", "env-value", "env-added", "service-account", "annotation"])
def test_anything_else_is_refused(change) -> None:
    after = copy.deepcopy(deployment())
    change(after)
    assert rs.non_image_changes({KEY: deployment()}, {KEY: after})


def test_added_or_removed_objects_are_refused() -> None:
    assert rs.non_image_changes({KEY: deployment()}, {})
    assert rs.non_image_changes({}, {KEY: deployment()})


def test_promotion_values_hold_only_image_keys() -> None:
    assert rs.promotion_values("llm-agent-mcp", {"mcp-tools": NEW}, SHA) == {
        "mcpServices": {"image": NEW, "identity": {"imageDigest": "sha256:" + "b" * 64, "gitSha": SHA}}}
    agents = rs.promotion_values("llm-agent", {"coordinator": NEW}, SHA)
    assert agents == {"agents": {"coordinator": {"byo": {"image": NEW}}}}


@pytest.mark.parametrize(("release", "images", "sha"), [
    ("llm-agent", {"coordinator": "repo/image:latest"}, SHA),
    ("llm-agent", {"mcp-tools": NEW}, SHA),
    ("llm-agent", {}, SHA),
    ("llm-agent", {"coordinator": NEW}, "short"),
    ("prompt-registry", {"coordinator": NEW}, SHA),
], ids=["tag", "wrong-release", "empty", "bad-sha", "unknown-release"])
def test_bad_promotion_inputs_are_refused(release, images, sha) -> None:
    with pytest.raises(rs.ReleaseStateError):
        rs.promotion_values(release, images, sha)


class FakeHelm:
    """Answers Helm reads from fixed manifests and records writes."""

    def __init__(self, deployed: str, rendered: list[str]) -> None:
        self.deployed, self.rendered, self.calls = deployed, list(rendered), []

    def __call__(self, command):
        self.calls.append(list(command))
        if command[:3] == ["helm", "get", "values"]:
            return "x: 1\n"
        if command[:3] == ["helm", "get", "manifest"]:
            return self.deployed
        if command[:2] == ["helm", "template"]:
            return self.rendered.pop(0)
        raise AssertionError(command)


def test_chart_that_differs_from_the_deployed_release_is_refused(tmp_path) -> None:
    deployed, local = yaml.safe_dump(deployment()), yaml.safe_dump(deployment(replicas=2))
    problems = rs.check_promotion(FakeHelm(deployed, [local, local]), "llm-agent-mcp", "c", tmp_path / "p", tmp_path / "v")
    assert problems and "differs from the deployed release" in problems[0]


def test_clean_promotion_passes_and_never_writes(tmp_path) -> None:
    helm = FakeHelm(yaml.safe_dump(deployment()), [yaml.safe_dump(deployment()), yaml.safe_dump(deployment(NEW))])
    assert rs.check_promotion(helm, "llm-agent-mcp", "c", tmp_path / "p", tmp_path / "v") == []
    assert not [call for call in helm.calls if "upgrade" in call]


def test_problems_never_print_values(tmp_path, capsys) -> None:
    after = deployment()
    after["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "super-secret-value"
    helm = FakeHelm(yaml.safe_dump(deployment()), [yaml.safe_dump(deployment()), yaml.safe_dump(after)])
    rs.report(rs.check_promotion(helm, "llm-agent-mcp", "c", tmp_path / "p", tmp_path / "v"))
    assert "super-secret-value" not in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [["promote"], ["promote", "--execute"]])
def test_promote_stops_without_both_approvals(monkeypatch, tmp_path, capsys, arguments) -> None:
    upgrades = []
    monkeypatch.delenv("PROMOTION_APPROVED", raising=False)
    monkeypatch.setattr(rs, "check_drift", lambda *args: [])
    monkeypatch.setattr(rs, "check_promotion", lambda *args: [])
    monkeypatch.setattr(rs, "promote", lambda *args: upgrades.append(args))
    code = rs.main([*arguments, "--release", "llm-agent-mcp", "--context", "c", "--promotion", str(tmp_path / "p")])
    assert code == 0 and upgrades == []
    assert "nothing changed" in capsys.readouterr().out


def test_failed_check_never_upgrades(monkeypatch, tmp_path) -> None:
    upgrades = []
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    monkeypatch.setattr(rs, "check_drift", lambda *args: ["drift"])
    monkeypatch.setattr(rs, "check_promotion", lambda *args: [])
    monkeypatch.setattr(rs, "promote", lambda *args: upgrades.append(args))
    code = rs.main(["promote", "--execute", "--release", "llm-agent-mcp", "--context", "c", "--promotion", str(tmp_path / "p")])
    assert code == 1 and upgrades == []


def test_upgrade_is_atomic_and_goes_through_helm(tmp_path) -> None:
    calls = []
    rs.promote(lambda command: calls.append(command) or "", "llm-agent", "c", tmp_path / "p", tmp_path / "v")
    assert calls[0][:2] == ["helm", "upgrade"] and "--atomic" in calls[0]
    assert calls[0][calls[0].index("--namespace") + 1] == "default"


@pytest.mark.skipif(not HELM, reason="helm binary unavailable")
def test_real_chart_digest_change_is_image_only() -> None:
    def rendered(digest: str) -> dict:
        overlay = {"agents": {"coordinator": {"byo": {"image": f"{REGISTRY}/g3-byo-coordinator@sha256:{digest * 64}"}}}}
        items = render(ON, BYO, overlay)
        return {(item["kind"], item["metadata"].get("namespace", ""), item["metadata"]["name"]): item for item in items}

    before, after = rendered("a"), rendered("b")
    assert rs.changes(before, after), "the digest must change something"
    assert rs.non_image_changes(before, after) == []


def test_tool_writes_nothing_outside_upgrade() -> None:
    text = Path(rs.__file__).read_text(encoding="utf-8")
    assert '"helm", "upgrade"' in text
    assert not any(word in text for word in ('"patch"', '"uninstall"', '"rollback"', '"apply"', '"delete"'))
