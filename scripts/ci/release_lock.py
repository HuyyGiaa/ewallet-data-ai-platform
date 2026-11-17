"""Resource-specific release locks as Kubernetes Leases in the CD namespace.

Jenkins jobs, manual runs and retries of different jobs can all target the same rehearsal
namespace or the same production releases. A Lease per lock name (``release-lock-<name>``) is
created create-only (POST, so an existing lock is a 409 and never overwritten) and deleted only at
the exact resourceVersion this holder created. An expired Lease, left by a killed build, is taken
over only by deleting that exact version first. Locks are acquired in sorted order, so two
releases needing overlapping locks can never deadlock.

This replaces relying on the controller having a single executor (P0 finding CICD-P0-F11).
Jenkins needs create/get/delete on ``leases`` in ``ewallet-cicd``
(``infra/kubernetes/jenkins/rbac/batch-b-isolated.yaml``; applying it is a separate approval).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Iterator, Protocol

from scripts.ci.k8s_api import KubeError

LOCK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
PREFIX = "release-lock-"


class LockError(RuntimeError):
    """A release lock is held by another release or could not be handled safely."""


class LeaseApi(Protocol):
    def create(self, document: dict) -> dict: ...
    def get(self, name: str) -> dict | None: ...
    def delete_exact(self, name: str, resource_version: str) -> None: ...


@dataclass
class KubeLeaseApi:
    """:class:`LeaseApi` over ``scripts/ci/k8s_api.py``."""

    client: object
    namespace: str

    def create(self, document: dict) -> dict:
        from scripts.ci.k8s_api import create

        return create(self.client, document)

    def get(self, name: str) -> dict | None:
        from scripts.ci.k8s_api import get

        try:
            return get(self.client, "Lease", self.namespace, name)
        except KubeError as error:
            if "HTTP 404" in str(error):
                return None
            raise

    def delete_exact(self, name: str, resource_version: str) -> None:
        from scripts.ci.k8s_api import delete_exact

        delete_exact(self.client, "Lease", self.namespace, name, resource_version)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def expired(lease: dict, now: datetime) -> bool:
    spec = lease.get("spec", {})
    started = _parse(spec.get("renewTime")) or _parse(spec.get("acquireTime"))
    duration = spec.get("leaseDurationSeconds")
    if started is None or not isinstance(duration, int) or duration <= 0:
        return False  # malformed: never take it over automatically
    return now > started + timedelta(seconds=duration)


@dataclass
class ReleaseLocks:
    api: LeaseApi
    namespace: str
    holder: str
    duration_seconds: int = 7200
    clock: Callable[[], datetime] = _now

    def _document(self, name: str) -> dict:
        now = _timestamp(self.clock())
        return {
            "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
            "metadata": {"name": PREFIX + name, "namespace": self.namespace,
                         "labels": {"app.kubernetes.io/part-of": "ewallet-cicd", "ewallet.io/release-lock": name}},
            "spec": {"holderIdentity": self.holder, "leaseDurationSeconds": self.duration_seconds,
                     "acquireTime": now, "renewTime": now},
        }

    def acquire_one(self, name: str) -> str:
        """Create the lock and return the resourceVersion this holder owns."""

        if not LOCK_NAME.fullmatch(name):
            raise LockError(f"invalid lock name {name}")
        for attempt in range(2):
            try:
                created = self.api.create(self._document(name))
                return str(created["metadata"]["resourceVersion"])
            except KubeError as error:
                if "HTTP 409" not in str(error):
                    raise LockError(f"cannot create release lock {name}: {error}") from error
            current = self.api.get(PREFIX + name)
            if current is None:
                continue  # released between the two calls; try once more
            holder = current.get("spec", {}).get("holderIdentity", "unknown")
            if attempt == 0 and expired(current, self.clock()):
                self.api.delete_exact(PREFIX + name, str(current["metadata"]["resourceVersion"]))
                continue
            raise LockError(f"release lock {name} is held by {holder}")
        raise LockError(f"release lock {name} could not be acquired")

    def release_one(self, name: str, resource_version: str) -> None:
        current = self.api.get(PREFIX + name)
        if current is None:
            return
        if current.get("spec", {}).get("holderIdentity") != self.holder:
            raise LockError(f"release lock {name} is no longer held by {self.holder}")
        self.api.delete_exact(PREFIX + name, resource_version)

    @contextmanager
    def hold(self, names: list[str] | set[str]) -> Iterator[list[str]]:
        acquired: list[tuple[str, str]] = []
        failed = False
        try:
            for name in sorted(set(names)):
                acquired.append((name, self.acquire_one(name)))
            yield [name for name, _ in acquired]
        except BaseException:
            failed = True
            raise
        finally:
            errors = []
            for name, version in reversed(acquired):
                try:
                    self.release_one(name, version)
                except (LockError, KubeError) as error:
                    errors.append(str(error))
            # A release failure stays the primary error; an unreleased lock is reported only
            # when nothing else failed (it expires after duration_seconds either way).
            if errors and not failed:
                raise LockError("; ".join(errors))
