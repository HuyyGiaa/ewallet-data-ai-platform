"""Production promotion through Helm: no-op refusal, approval gates, readback and reverse-order rollback."""

from __future__ import annotations

import json

import pytest
import yaml

from scripts.ci import promote
from scripts.ci import release_state as rs

SHA = "c" * 40
REG = "asia-southeast1-docker.pkg.dev/p/ewallet-platform"
OLD = {"mcp-tools": f"{REG}/mcp-tools@sha256:" + "a" * 64, "coordinator": f"{REG}/g3@sha256:" + "a" * 64}
NEW = {"mcp-tools": f"{REG}/mcp-tools@sha256:" + "b" * 64, "g3-byo-coordinator": f"{REG}/g3@sha256:" + "b" * 64}


def deployment(name: str, image: str) -> str:
    return yaml.safe_dump({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": "n"},
                           "spec": {"template": {"spec": {"containers": [{"name": "c", "image": image}]}}}})


class FakeCluster:
    """Two Helm releases whose rendered image follows the last -f overlay that sets one."""

    def __init__(self, fail_upgrade_of: str | None = None, drift_after: str | None = None) -> None:
        self.images = {"llm-agent-mcp": OLD["mcp-tools"], "llm-agent": OLD["coordinator"]}
        self.revisions = {"llm-agent-mcp": 19, "llm-agent": 59}
        self.calls: list[list[str]] = []
        self.fail_upgrade_of, self.drift_after = fail_upgrade_of, drift_after
        self.upgraded: set[str] = set()
        self.states: dict[str, str] = {}
        # Failure injection: an upgrade that changes the cluster and then fails (timeout, lost
        # result), a rollback that fails, a rollback that leaves the release not deployed.
        self.partial_upgrade_of: str | None = None
        self.partial_state: str = "pending-upgrade"
        self.rollback_image: str | None = None  # a rollback that "succeeds" with the wrong image
        self.history_error: set[str] = set()
        self.fail_rollback_of: str | None = None
        self.rollback_state: str = "deployed"

    def render_image(self, release: str, files: list[str]) -> str:
        image = self.images[release]
        for path in files:
            data = yaml.safe_load(open(path)) or {}
            image = (data.get("mcpServices", {}).get("image") or data.get("agents", {}).get("coordinator", {})
                     .get("byo", {}).get("image") or image)
        return image

    def __call__(self, command):  # type: ignore[no-untyped-def]
        command = list(command)
        self.calls.append(command)
        if command[:2] == ["helm", "history"]:
            if command[2] in self.history_error:
                raise rs.ReleaseStateError("helm history failed with exit 1")
            return json.dumps([{"revision": self.revisions[command[2]], "status": self.states.get(command[2], "deployed")}])
        if command[:3] == ["helm", "get", "values"]:
            return "x: 1\n"
        if command[:3] == ["helm", "get", "manifest"]:
            return deployment(command[3], self.images[command[3]])
        if command[:2] == ["helm", "template"]:
            files = [command[i + 1] for i, item in enumerate(command) if item == "-f"]
            return deployment(command[2], self.render_image(command[2], files))
        if command[:2] == ["helm", "upgrade"]:
            release = command[2]
            if release == self.fail_upgrade_of:
                raise rs.ReleaseStateError("helm upgrade failed with exit 1")
            if release == self.partial_upgrade_of:
                files = [command[i + 1] for i, item in enumerate(command) if item == "-f"]
                self.images[release] = self.render_image(release, files)
                self.revisions[release] += 1
                self.states[release] = self.partial_state
                raise rs.AmbiguousMutationError("helm upgrade timed out after 1500s; the release state is unknown")
            files = [command[i + 1] for i, item in enumerate(command) if item == "-f"]
            self.images[release] = self.render_image(release, files)
            self.revisions[release] += 1
            self.upgraded.add(release)
            return ""
        if command[:2] == ["helm", "rollback"]:
            release = command[2]
            if release == self.fail_rollback_of:
                raise rs.ReleaseStateError("helm rollback failed with exit 1")
            self.images[release] = self.rollback_image or (OLD["mcp-tools"] if release == "llm-agent-mcp"
                                                           else OLD["coordinator"])
            self.revisions[release] += 1  # a rollback is a new revision
            self.states[release] = self.rollback_state
            return ""
        if command[0] == "kubectl":
            release = "llm-agent-mcp" if "mcp" in " ".join(command) else "llm-agent"
            image = self.drift_after if self.drift_after and release in self.upgraded else self.images[release]
            return json.dumps({"kind": "Deployment", "spec": {"template": {"spec": {"containers": [{"image": image}]}}}})
        raise AssertionError(command)


def plan(*names: str) -> dict:
    units = {
        "mcp-servers-production": {"name": "mcp-servers-production", "kind": "helm-image-promotion",
                                   "helm": {"release": "llm-agent-mcp", "namespace": "ewallet-mcp",
                                            "image_values": {"mcp-tools": "mcp-tools"}},
                                   "images": {"mcp-tools": NEW["mcp-tools"]}},
        "coordinator-production": {"name": "coordinator-production", "kind": "helm-image-promotion",
                                   "helm": {"release": "llm-agent", "namespace": "default",
                                            "image_values": {"g3-byo-coordinator": "coordinator"}},
                                   "images": {"g3-byo-coordinator": NEW["g3-byo-coordinator"]}},
    }
    return {"stage": "production", "git_sha": SHA, "units": [units[name] for name in names]}


@pytest.fixture(autouse=True)
def approved(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")


def test_both_units_are_upgraded_in_order_and_verified() -> None:
    cluster = FakeCluster()
    report = promote.promote_plan(cluster, plan("mcp-servers-production", "coordinator-production"), "prod",
                                  execute=True, verify=lambda units: True)
    assert report["verdict"] == "PASS" and report["production_changed"]
    upgrades = [call[2] for call in cluster.calls if call[:2] == ["helm", "upgrade"]]
    assert upgrades == ["llm-agent-mcp", "llm-agent"]
    assert cluster.images == {"llm-agent-mcp": NEW["mcp-tools"], "llm-agent": NEW["g3-byo-coordinator"]}


def test_failed_verification_rolls_back_every_upgrade_in_reverse_order() -> None:
    cluster = FakeCluster()
    report = promote.promote_plan(cluster, plan("mcp-servers-production", "coordinator-production"), "prod",
                                  execute=True, verify=lambda units: False)
    assert report["verdict"] == "FAIL" and "runtime verification failed" in report["failure"]
    rollbacks = [(call[2], call[3]) for call in cluster.calls if call[:2] == ["helm", "rollback"]]
    assert rollbacks == [("llm-agent", "59"), ("llm-agent-mcp", "19")]


def test_a_later_unit_failing_rolls_back_the_earlier_one() -> None:
    cluster = FakeCluster(fail_upgrade_of="llm-agent")
    report = promote.promote_plan(cluster, plan("mcp-servers-production", "coordinator-production"), "prod",
                                  execute=True, verify=lambda units: True)
    assert report["verdict"] == "FAIL"
    assert [(call[2], call[3]) for call in cluster.calls if call[:2] == ["helm", "rollback"]] == [("llm-agent-mcp", "19")]


def test_a_failed_post_upgrade_readback_is_rolled_back() -> None:
    cluster = FakeCluster(drift_after=f"{REG}/mcp-tools@sha256:" + "f" * 64)
    report = promote.promote_plan(cluster, plan("mcp-servers-production"), "prod", execute=True)
    assert report["verdict"] == "FAIL"
    assert [call[2] for call in cluster.calls if call[:2] == ["helm", "rollback"]] == ["llm-agent-mcp"]


def test_a_digest_already_running_is_already_current_and_not_upgraded() -> None:
    cluster = FakeCluster()
    cluster.images["llm-agent-mcp"] = NEW["mcp-tools"]
    report = promote.promote_plan(cluster, plan("mcp-servers-production"), "prod", execute=True)
    assert report["verdict"] == "PASS" and report["units"][0]["status"] == "ALREADY_CURRENT"
    assert not [call for call in cluster.calls if call[:2] == ["helm", "upgrade"]]
    assert report["production_changed"] is False


def test_without_execute_or_approval_nothing_is_written(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cluster = FakeCluster()
    report = promote.promote_plan(cluster, plan("mcp-servers-production"), "prod", execute=False)
    assert report["verdict"] == "CHECKED_NOT_EXECUTED"
    monkeypatch.setenv("PROMOTION_APPROVED", "no")
    report = promote.promote_plan(cluster, plan("mcp-servers-production"), "prod", execute=True)
    assert report["units"][0]["status"] == "CHECKED_NOT_EXECUTED"
    assert not [call for call in cluster.calls if call[:2] in (["helm", "upgrade"], ["helm", "rollback"])]


@pytest.mark.parametrize("bad", [dict(stage="rehearsal"), dict(context="in-cluster"), dict(context=""),
                                 dict(kind="fleet-image-promotion")])
def test_only_production_helm_units_with_an_explicit_context_are_promoted(bad) -> None:  # type: ignore[no-untyped-def]
    data = plan("mcp-servers-production")
    data["stage"] = bad.get("stage", "production")
    if "kind" in bad:
        data["units"][0]["kind"] = bad["kind"]
    context = bad.get("context", "prod")
    if "kind" in bad:
        assert promote.promote_plan(FakeCluster(), data, context, execute=True)["verdict"] == "FAIL"
    else:
        with pytest.raises(promote.PromotionError):
            promote.promote_plan(FakeCluster(), data, context, execute=True)
