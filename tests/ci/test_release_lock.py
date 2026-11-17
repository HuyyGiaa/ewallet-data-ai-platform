"""Release locks as Leases: create-only, exact-version release, expired takeover, sorted acquisition."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.ci.k8s_api import KubeError
from scripts.ci.release_lock import LockError, ReleaseLocks, expired

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeLeases:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.version = 0
        self.calls: list[tuple[str, str]] = []

    def create(self, document: dict) -> dict:
        name = document["metadata"]["name"]
        self.calls.append(("create", name))
        if name in self.objects:
            raise KubeError(f"POST leases -> HTTP 409: {name} already exists")
        self.version += 1
        stored = {**document, "metadata": {**document["metadata"], "resourceVersion": str(self.version)}}
        self.objects[name] = stored
        return stored

    def get(self, name: str):  # type: ignore[no-untyped-def]
        return self.objects.get(name)

    def delete_exact(self, name: str, resource_version: str) -> None:
        self.calls.append(("delete", name))
        if self.objects.get(name, {}).get("metadata", {}).get("resourceVersion") != resource_version:
            raise KubeError("HTTP 409: precondition failed")
        del self.objects[name]


def locks(api: FakeLeases, holder: str = "build-1", clock=lambda: NOW) -> ReleaseLocks:  # type: ignore[no-untyped-def]
    return ReleaseLocks(api, "ewallet-cicd", holder, duration_seconds=600, clock=clock)


def test_locks_are_held_in_sorted_order_and_released() -> None:
    api = FakeLeases()
    with locks(api).hold({"production-release", "cicd-agent-platform"}) as names:
        assert names == ["cicd-agent-platform", "production-release"]
        assert set(api.objects) == {"release-lock-cicd-agent-platform", "release-lock-production-release"}
    assert api.objects == {}


def test_a_held_lock_refuses_a_second_release() -> None:
    api = FakeLeases()
    with locks(api, "build-1").hold(["cicd-agent-platform"]):
        with pytest.raises(LockError, match="held by build-1"):
            with locks(api, "build-2").hold(["cicd-agent-platform"]):
                pass
    assert api.objects == {}


def test_an_expired_lock_left_by_a_killed_build_is_taken_over() -> None:
    api = FakeLeases()
    stale = locks(api, "build-0", clock=lambda: NOW - timedelta(hours=3))
    stale.acquire_one("cicd-rag-pipeline")
    with locks(api, "build-1").hold(["cicd-rag-pipeline"]):
        assert api.objects["release-lock-cicd-rag-pipeline"]["spec"]["holderIdentity"] == "build-1"


def test_a_release_failure_stays_the_primary_error_and_locks_are_released() -> None:
    api = FakeLeases()
    with pytest.raises(RuntimeError, match="deploy failed"):
        with locks(api).hold(["cicd-agent-platform"]):
            raise RuntimeError("deploy failed")
    assert api.objects == {}


def test_partial_acquisition_releases_what_it_took() -> None:
    api = FakeLeases()
    locks(api, "other").acquire_one("production-release")
    with pytest.raises(LockError):
        with locks(api).hold(["cicd-agent-platform", "production-release"]):
            pass
    assert set(api.objects) == {"release-lock-production-release"}


def test_malformed_leases_never_expire_and_names_are_validated() -> None:
    assert not expired({"spec": {"holderIdentity": "x"}}, NOW)
    with pytest.raises(LockError, match="invalid lock name"):
        locks(FakeLeases()).acquire_one("Bad Name")
