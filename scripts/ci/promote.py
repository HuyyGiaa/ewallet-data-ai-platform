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


def _restored(run: Runner, unit: UnitPromotion, context: str) -> list[str]:
    """Problems with a finished rollback: it must be deployed AND restore the recorded configuration."""

    after = latest_release(run, unit.release, unit.namespace, context)
    problems = []
    if after.get("status") != "deployed":
        problems.append(f"the latest revision {after.get('revision')} is {after.get('status')}, not deployed")
    digest, images = manifest_fingerprint(run, unit.release, context)
    if images != unit.previous_images:
        problems.append("the deployed images are not the pre-promotion images")
    elif digest != unit.previous_manifest_sha256:
        problems.append("the deployed manifest differs from the pre-promotion manifest")
    return problems


def rollback(run: Runner, promoted: Sequence[UnitPromotion], context: str) -> list[str]:
    """Roll back every unit this run may have changed, in reverse order; return the errors (never raises).

    For an attempted or confirmed upgrade the release is inspected first (latest revision, its status,
    the deployed manifest against the pre-promotion snapshot):

    * same revision, ``deployed``, same manifest -> ``NOT_CHANGED`` (unchanged and healthy);
    * newer revision, ``deployed`` or ``failed`` -> ``helm rollback`` to the recorded revision, then the
      release must be ``deployed`` and run the recorded manifest and images (``ROLLED_BACK``), otherwise
      ``ROLLBACK_FAILED``;
    * anything else (a pending state, an older revision, unreadable history, same revision but not
      deployed or a different manifest) -> ``STATE_UNKNOWN``: no automated action, recovery by hand.
    """

    errors = []
    for unit in reversed(promoted):
        if unit.status not in MUTATED or unit.previous_revision is None:
            continue
        try:
            latest = latest_release(run, unit.release, unit.namespace, context)
        except (rs.ReleaseStateError, PromotionError, ValueError, OSError) as error:
            unit.status = "STATE_UNKNOWN"
            errors.append(f"{unit.name}: STATE_UNKNOWN: Helm history unreadable after the failure: {error}")
            continue
        revision, state = latest["revision"], latest.get("status")
        try:
            if revision == unit.previous_revision:
                digest = manifest_fingerprint(run, unit.release, context)[0] if state == "deployed" else None
                if state == "deployed" and digest == unit.previous_manifest_sha256:
                    unit.status = "NOT_CHANGED"
                    continue
                unit.status = "STATE_UNKNOWN"
                errors.append(f"{unit.name}: STATE_UNKNOWN: revision {revision} unchanged but {state}"
                              + (", manifest changed" if state == "deployed" else ""))
                continue
            if revision < unit.previous_revision or state not in ROLLBACK_FROM:
                unit.status = "STATE_UNKNOWN"
                errors.append(f"{unit.name}: STATE_UNKNOWN: revision {revision} is {state}; no automated rollback")
                continue
            unit.detail.append(f"rollback from revision {revision} ({state}) to {unit.previous_revision}")
            run(["helm", "rollback", unit.release, str(unit.previous_revision), "--kube-context", context,
                 "--namespace", unit.namespace, "--wait", "--timeout", "15m"])
            problems = _restored(run, unit, context)
            if problems:
                raise PromotionError("; ".join(problems))
            unit.status = "ROLLED_BACK"
        except (rs.ReleaseStateError, PromotionError, ValueError, OSError) as error:
            unit.status = "ROLLBACK_FAILED"
            errors.append(f"{unit.name}: rollback to revision {unit.previous_revision} failed: {error}")
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
