"""Production promotion of ``helm-image-promotion`` release units (image-only, through Helm).

For each unit, in plan order:

1. record the release's current revision (the rollback target);
2. ``release_state`` checks: running images equal Helm's record, the local chart reproduces the
   deployed manifest, and the promotion changes nothing but image identity;
3. refuse a no-op: the promotion must change an image to the requested digest, unless the
   release already runs exactly that digest (``ALREADY_CURRENT``, nothing is upgraded);
4. ``helm upgrade --atomic`` with the live values plus the promotion overlay;
5. read back: Helm's record and the running workloads now run the requested digest.

If a later unit or the runtime verification fails, every unit this run upgraded is rolled back
(``helm rollback <release> <recorded revision>``) in reverse order; the original failure stays
the reported cause. An upgrade is recorded as ``UPGRADE_ATTEMPTED`` before ``helm upgrade`` runs,
so a failed, timed-out or lost upgrade is never mistaken for "nothing changed": the rollback reads
the release history and rolls back whenever the latest revision is no longer the recorded one. Nothing runs without ``--execute`` and ``PROMOTION_APPROVED=yes``.
Production credentials are never implied: the caller passes an explicit kube context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence

import yaml

from scripts.ci import release_state as rs

Runner = Callable[[Sequence[str]], str]


class PromotionError(RuntimeError):
    """A promotion step failed; the message holds no setting values."""


@dataclass
class UnitPromotion:
    name: str
    release: str
    namespace: str
    previous_revision: int | None = None
    status: str = "PENDING"
    detail: list[str] = field(default_factory=list)
    # Pre-mutation snapshot of the deployed manifest: a digest and the image set, never values or Secrets.
    previous_manifest_sha256: str | None = None
    previous_images: list[str] = field(default_factory=list)


def manifest_fingerprint(run: Runner, release: str, context: str) -> tuple[str, list[str]]:
    """(sha256 of the canonical deployed manifest, sorted images); compares configuration without storing it."""

    documents = rs.load_documents(rs._helm_read(run, "manifest", release, context))
    canonical = json.dumps(sorted([list(key), document] for key, document in documents.items()), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest(), sorted(rs.rendered_images(documents))


def latest_release(run: Runner, release: str, namespace: str, context: str) -> dict[str, Any]:
    """The latest Helm history entry (revision and status)."""

    history = json.loads(run(["helm", "history", release, "--kube-context", context, "--namespace", namespace,
                              "--max", "1", "--output", "json"]) or "[]")
    if not history or not isinstance(history[-1].get("revision"), int):
        raise PromotionError(f"{release} has no readable Helm revision")
    return history[-1]


def current_revision(run: Runner, release: str, namespace: str, context: str) -> int:
    return latest_release(run, release, namespace, context)["revision"]


def promote_unit(run: Runner, unit: Mapping[str, Any], git_sha: str, context: str, workdir: Path,
                 execute: bool, record: list[UnitPromotion],
                 before_mutation: Callable[[UnitPromotion], None] | None = None) -> UnitPromotion:
    """Promote one unit. The result is appended to ``record`` before any mutation, so a failure after
    ``helm upgrade`` still leaves the unit for rollback."""

    helm = unit["helm"]
    release, namespace = helm["release"], helm["namespace"]
    if rs.RELEASE_NAMESPACE.get(release) != namespace:
        raise PromotionError(f"{unit['name']}: {release} is not a promotable release in {namespace}")
    result = UnitPromotion(unit["name"], release, namespace)
    record.append(result)
    images = {helm["image_values"][image]: reference for image, reference in unit["images"].items()}
    overlay = workdir / f"{unit['name']}-promotion.yaml"
    overlay.write_text(yaml.safe_dump(rs.promotion_values(release, images, git_sha), sort_keys=True), encoding="utf-8")
    live_values = workdir / f"{unit['name']}-live-values.yaml"
    latest = latest_release(run, release, namespace, context)
    result.previous_revision = latest["revision"]
    if latest.get("status") != "deployed":
        # Another operation is pending or the release is failed: never start a promotion on top of it.
        result.status = "REFUSED"
        raise PromotionError(f"{unit['name']}: {release} revision {latest['revision']} is {latest.get('status')}, "
                             "not deployed")
    result.previous_manifest_sha256, result.previous_images = manifest_fingerprint(run, release, context)
    problems = [*rs.check_drift(run, release, context),
                *rs.check_promotion(run, release, context, overlay, live_values)]
    if problems:
        result.status, result.detail = "REFUSED", problems
        raise PromotionError(f"{unit['name']}: " + "; ".join(problems[:5]))
    before = rs.render(run, release, [live_values])
    after = rs.render(run, release, [live_values, overlay])
    effect = rs.promotion_effect(before, after, images.values())
    if effect == "ALREADY_CURRENT":
        result.status = "ALREADY_CURRENT"
        return result
    if not execute or os.environ.get("PROMOTION_APPROVED") != "yes":
        result.status = "CHECKED_NOT_EXECUTED"
        return result
    result.status = "UPGRADE_ATTEMPTED"  # from here on the cluster may have changed
    if before_mutation is not None:
        before_mutation(result)  # e.g. a durable journal entry; a failure here stops before helm runs
    rs.promote(run, release, context, overlay, live_values)
    result.status = "UPGRADED"
    remaining = rs.check_drift(run, release, context)
    deployed = rs.rendered_images(rs.load_documents(rs._helm_read(run, "manifest", release, context)))
    if remaining or not set(images.values()) <= deployed:
        result.detail = remaining or ["the deployed manifest does not run the requested digest"]
        raise PromotionError(f"{unit['name']}: post-upgrade readback failed")
    return result


MUTATED = frozenset({"UPGRADE_ATTEMPTED", "UPGRADED"})
# Helm states from which an automated rollback to the recorded revision is safe. A pending state means
# another operation may still be running: a destructive retry is never attempted.
ROLLBACK_FROM = frozenset({"deployed", "failed"})
# The single Helm recovery policy, used by the automated rollback below AND by the operator recovery plan
# (scripts/ci/production_release.py recovery_plan). Human-readable form of rollback_decision.
RECOVERY_RULES = (
    "NOT_CHANGED: latest revision == recorded, status deployed, manifest SHA-256 and images == recorded -> no action",
    "RESTORED: latest revision is recorded+1 or recorded+2 (helm --atomic or a finished rollback), status deployed, "
    "manifest SHA-256 and images == recorded -> no action",
    "ROLLBACK: latest revision == recorded+1, status deployed or failed, manifest readable and not the recorded one, "
    "not after a rollback -> helm rollback <release> <recorded revision> --wait, then re-observe: the result must "
    "be RESTORED (deployed, recorded manifest SHA-256 and images); Helm's exit code alone never completes recovery",
    "STATE_UNKNOWN: anything else - a pending-* or other status, an older revision, more than recorded+2 or "
    "recorded+2 not restored (concurrent modification), unreadable history or manifest, missing pre-mutation "
    "fingerprint, same revision not deployed or with another manifest, a rollback that did not restore -> no "
    "automated or recommended rollback; manual inspection and a separately authorized decision",
)


def rollback_decision(previous: Mapping[str, Any], latest: Mapping[str, Any] | None,
                      current: Mapping[str, Any] | None, *, after_rollback: bool = False) -> tuple[str, str]:
    """Pure Helm recovery decision: (NOT_CHANGED | RESTORED | ROLLBACK | STATE_UNKNOWN, reason).

    ``previous``: the pre-mutation snapshot (``revision``, ``manifest_sha256``, ``images``);
    ``latest``: the latest history entry (``revision``, ``status``) or None when unreadable;
    ``current``: the deployed manifest fingerprint (``manifest_sha256``, ``images``) or None when unreadable.
    """

    revision, digest, images = previous.get("revision"), previous.get("manifest_sha256"), previous.get("images")
    if not isinstance(revision, int) or not digest or not images:
        return "STATE_UNKNOWN", "no pre-mutation fingerprint recorded (revision, manifest SHA-256, images)"
    if not latest or not isinstance(latest.get("revision"), int):
        return "STATE_UNKNOWN", "Helm history unreadable"
    now, status = latest["revision"], latest.get("status")
    restored = bool(current) and current.get("manifest_sha256") == digest and sorted(current.get("images") or []) == sorted(images)
    if now < revision:
        return "STATE_UNKNOWN", f"revision {now} is older than the recorded {revision} (concurrent modification)"
    if now > revision + 2:
        return "STATE_UNKNOWN", f"{now - revision} revisions since the recorded {revision} (concurrent modification)"
    if status not in ROLLBACK_FROM:
        return "STATE_UNKNOWN", f"revision {now} is {status}"
    if now == revision:
        if status == "deployed" and restored:
            return "NOT_CHANGED", "unchanged and healthy"
        return "STATE_UNKNOWN", f"revision {now} unchanged but " + ("the manifest changed" if current else
                                                                    f"{status} or manifest unreadable")
    if status == "deployed" and restored:
        return "RESTORED", f"revision {now} is deployed with the recorded manifest and images"
    if after_rollback:
        return "STATE_UNKNOWN", "the rollback did not restore the recorded manifest, images and deployed status"
    if current is None:
        return "STATE_UNKNOWN", "the deployed manifest is unreadable"
    if now == revision + 2:
        return "STATE_UNKNOWN", f"revision {now} (recorded+2) does not run the recorded configuration"
    return "ROLLBACK", f"revision {now} ({status}) differs from the recorded {revision}"


def observe(run: Runner, release: str, namespace: str, context: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read-only observation (history entry, manifest fingerprint); None for what cannot be read."""

    try:
        latest: dict[str, Any] | None = latest_release(run, release, namespace, context)
    except (rs.ReleaseStateError, PromotionError, ValueError, OSError):
        latest = None
    try:
        digest, images = manifest_fingerprint(run, release, context)
        current: dict[str, Any] | None = {"manifest_sha256": digest, "images": images}
    except (rs.ReleaseStateError, ValueError, OSError):
        current = None
    return latest, current


def snapshot(unit: UnitPromotion) -> dict[str, Any]:
    return {"revision": unit.previous_revision, "manifest_sha256": unit.previous_manifest_sha256,
            "images": list(unit.previous_images)}


def rollback(run: Runner, promoted: Sequence[UnitPromotion], context: str) -> list[str]:
    """Roll back every unit this run may have changed, in reverse order; return the errors (never raises).

    Each attempted or confirmed upgrade is observed and decided by :func:`rollback_decision` (the same
    policy as the operator recovery plan): NOT_CHANGED and RESTORED need no action; ROLLBACK runs
    ``helm rollback`` and must then decide RESTORED (``ROLLED_BACK``), else ``ROLLBACK_FAILED``;
    STATE_UNKNOWN takes no automated action.
    """

    errors = []
    for unit in reversed(promoted):
        if unit.status not in MUTATED or unit.previous_revision is None:
            continue
        latest, current = observe(run, unit.release, unit.namespace, context)
        decision, reason = rollback_decision(snapshot(unit), latest, current)
        if decision == "NOT_CHANGED":
            unit.status = "NOT_CHANGED"
            continue
        if decision == "RESTORED":
            unit.status = "ROLLED_BACK"
            unit.detail.append(f"already restored ({reason})")
            continue
        if decision == "STATE_UNKNOWN":
            unit.status = "STATE_UNKNOWN"
            errors.append(f"{unit.name}: STATE_UNKNOWN: {reason}; no automated rollback")
            continue
        unit.detail.append(f"rollback from revision {latest['revision']} ({latest.get('status')}) to {unit.previous_revision}")
        try:
            run(["helm", "rollback", unit.release, str(unit.previous_revision), "--kube-context", context,
                 "--namespace", unit.namespace, "--wait", "--timeout", "15m"])
        except (rs.ReleaseStateError, ValueError, OSError) as error:
            unit.status = "ROLLBACK_FAILED"
            errors.append(f"{unit.name}: rollback to revision {unit.previous_revision} failed: {error}")
            continue
        after, verdict = rollback_decision(snapshot(unit), *observe(run, unit.release, unit.namespace, context),
                                           after_rollback=True)
        if after == "RESTORED":
            unit.status = "ROLLED_BACK"
        else:
            unit.status = "ROLLBACK_FAILED"
            errors.append(f"{unit.name}: rollback to revision {unit.previous_revision} failed: {verdict}")
    return errors


def promote_plan(run: Runner, plan: Mapping[str, Any], context: str, *, execute: bool,
                 verify: Callable[[list[UnitPromotion]], bool] | None = None) -> dict[str, Any]:
    """Promote every helm-image-promotion unit of a verified production plan."""

    if plan.get("stage") != "production":
        raise PromotionError("only a production plan can be promoted")
    if not context or context == "in-cluster":
        raise PromotionError("an explicit production kube context is required")
    promoted: list[UnitPromotion] = []
    failure = ""
    with tempfile.TemporaryDirectory(prefix="promote.") as directory:
        try:
            for unit in plan["units"]:
                if unit["kind"] != "helm-image-promotion":
                    raise PromotionError(f"{unit['name']}: kind {unit['kind']} is not promotable automatically")
                promote_unit(run, unit, plan["git_sha"], context, Path(directory), execute, promoted)
            if execute and verify is not None and not verify(promoted):
                raise PromotionError("runtime verification failed after promotion")
        except (PromotionError, rs.ReleaseStateError) as error:
            failure = str(error)
    rollback_errors = rollback(run, promoted, context) if failure else []
    verdict = "PASS" if not failure else "FAIL"
    if verdict == "PASS" and not execute:
        verdict = "CHECKED_NOT_EXECUTED"
    return {
        "verdict": verdict, "stage": "production", "git_sha": plan["git_sha"],
        "production_changed": any(unit.status in {"UPGRADED", "ROLLED_BACK", "ROLLBACK_FAILED"} for unit in promoted),
        "failure": failure or None, "rollback_errors": rollback_errors,
        "units": [unit.__dict__ for unit in promoted],
    }
