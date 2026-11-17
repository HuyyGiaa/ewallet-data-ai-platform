"""Agent Registry lifecycle of a production release: requirement, then fail-closed reconciliation.

Kubernetes and agentregistry cannot change atomically together, so the release states the gap instead
of hiding it. The production transaction (``production_release.py``) ends in one of:

* ``RELEASE_COMPLETE``            runtime verified and no registered identity changed;
* ``DEPLOYED_PENDING_REGISTRY``   runtime verified; the Coordinator and/or fleet image changed, so
                                  the registry's compared digest labels are stale until the separately
                                  authorized publication (CI/CD never publishes);

and this module moves a ``DEPLOYED_PENDING_REGISTRY`` release to:

* ``RELEASE_COMPLETE``                  publication done and a strict readback proves it, or
* ``REGISTRY_RECONCILIATION_FAILED``    anything less (the runtime stays as verified; nothing is rolled
                                        back automatically, because the runtime is healthy and the
                                        publication has its own approval).

Why publication is a separate, recorded step (ops/llm_agent/registry_catalog.py, frozen):

* Agent entries carry ``ewallet.io/byo-image-digest`` (Coordinator) and
  ``ewallet.io/sandbox-image-digest`` (Specialists) as compared labels; ``ewallet.io/git-sha`` is a
  publication label;
* an image promotion does not change an Agent's tag (its chart version), and ``/v0/apply`` upserts,
  so publication OVERWRITES the record at the same tag. Tags are not immutable. The previous record
  must therefore be captured (a strict readback before publishing) so a partial or wrong publication
  can be restored by re-publishing the previous catalog (previous live values and previous fleet image,
  both in the release journal). Records are never deleted to make a readback pass.

``reconcile`` accepts only: the pre-publication capture with every changed Agent registered, a strict
post-publication readback (verdict PASS, every entry ``matches``, rendered at the release SHA), and the
changed Agents published at the release SHA. Combined with the runtime identity rows (live images are
the plan digests), a matching readback proves the registry labels are the plan digests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

COORDINATOR_AGENT = "fraud-coordinator"
SPECIALIST_AGENTS = ("drift-specialist", "feature-rag-specialist")
REGISTRY_FOLLOWUP = (
    "Agent Registry entries carry the Coordinator BYO image digest and the SandboxAgent fleet image digest as "
    "compared labels (ops/llm_agent/registry_catalog.py). A promotion makes them stale until the separately "
    "authorized publication; publication overwrites the record at the unchanged tag, so capture it first. "
    "CI/CD never publishes.")


def requirement(plan: Mapping[str, Any], *, coordinator_changed: bool, fleet_changed: bool) -> dict[str, Any]:
    """What the registry needs after a verified promotion (pure; used by the transaction)."""

    agents = sorted(([COORDINATOR_AGENT] if coordinator_changed else []) + (list(SPECIALIST_AGENTS) if fleet_changed else []))
    if not agents:
        return {"status": "NOT_REQUIRED", "reason": "no registered image identity changed", "agents": []}
    fleet_image = next((u["images"]["g3-byo-coordinator"] for u in plan["units"]
                        if u["kind"] == "fleet-image-promotion"), "<live fleet image>")
    common = ["--values-overlay", "infra/helm/llm-agent-platform/g3/g3.yaml",
              "--values-overlay", "infra/helm/llm-agent-platform/agi/agents.yaml",
              "--values-overlay", "<live llm-agent values>", "--sandbox-image", fleet_image,
              "--git-sha", plan["git_sha"]]
    return {
        "status": "PENDING_AUTHORIZATION", "reason": REGISTRY_FOLLOWUP, "agents": agents,
        "release_sha": plan["git_sha"],
        "procedure": [
            {"step": "capture", "authorization": "read-only",
             "command": ["python", "-m", "ops.llm_agent.registry_catalog", "readback", "--registry", "<registry>",
                         *common, "--evidence", "<REPLAY_DIR>/registry/readback-before.json"]},
            {"step": "dry-run", "authorization": "read-only",
             "command": ["python", "-m", "ops.llm_agent.registry_catalog", "publish", "--registry", "<registry>",
                         "--dry-run", *common]},
            {"step": "publish", "authorization": "separate owner approval (production registry write)",
             "command": ["python", "-m", "ops.llm_agent.registry_catalog", "publish", "--registry", "<registry>",
                         "--confirm-live-write", "<registry>", *common]},
            {"step": "readback", "authorization": "read-only",
             "command": ["scripts/llm_agent/agent_replay.sh", "registry"]},
            {"step": "reconcile", "authorization": "offline",
             "command": ["python", "-m", "scripts.ci.registry_release", "reconcile", "--release-verdict",
                         "<release-verdict.json>", "--before", "<REPLAY_DIR>/registry/readback-before.json",
                         "--after", "<REPLAY_DIR>/registry/readback.json", "--output", "<registry-verdict.json>"]},
        ],
        "recovery": ("If publication fails or the readback mismatches: do not delete records. Re-publish the "
                     "captured previous catalog (previous live values and fleet image from the release journal) "
                     "with the same approval, read back, and record REGISTRY_RECONCILIATION_FAILED until it matches."),
    }


def _entries(report: Any) -> list[dict[str, Any]] | None:
    if not isinstance(report, dict) or not isinstance(report.get("entries"), list):
        return None
    return [entry for entry in report["entries"] if isinstance(entry, dict)]


def reconcile(release_verdict: Mapping[str, Any], before: Any, after: Any) -> dict[str, Any]:
    """Fail-closed: RELEASE_COMPLETE only with a capture, a strict readback and the changed Agents at the SHA."""

    state = release_verdict.get("verdict")
    registry = release_verdict.get("registry") or {}
    sha = release_verdict.get("git_sha")
    reasons: list[str] = []
    if state == "RELEASE_COMPLETE" and registry.get("status") == "NOT_REQUIRED":
        return {"verdict": "RELEASE_COMPLETE", "git_sha": sha, "reasons": [], "registry": "NOT_REQUIRED"}
    if state != "DEPLOYED_PENDING_REGISTRY" or registry.get("status") != "PENDING_AUTHORIZATION":
        return {"verdict": "REGISTRY_RECONCILIATION_FAILED", "git_sha": sha,
                "reasons": [f"the release is {state} with registry {registry.get('status')}; only a verified "
                            "DEPLOYED_PENDING_REGISTRY release can be reconciled"]}
    agents = list(registry.get("agents") or [])
    captured = _entries(before)
    if captured is None:
        reasons.append("NO_PRE_PUBLICATION_CAPTURE: the previous registry records were not captured (no recovery path)")
    else:
        previous = {e.get("name"): e for e in captured if e.get("kind") == "Agent"}
        missing = [name for name in agents if not previous.get(name, {}).get("registered")]
        if missing:
            reasons.append(f"CAPTURE_INCOMPLETE: no previous record captured for {missing}")
    entries = _entries(after)
    if entries is None:
        reasons.append("NO_READBACK: the post-publication readback is missing or malformed")
    else:
        if after.get("verdict") != "PASS":
            reasons.append(f"READBACK_FAILED: strict readback verdict {after.get('verdict')}")
        if after.get("git_sha") != sha:
            reasons.append(f"READBACK_WRONG_RELEASE: rendered at {after.get('git_sha')}, not the release {sha}")
        mismatched = [f"{e.get('kind')}/{e.get('name')}" for e in entries if not e.get("matches")]
        if mismatched:
            reasons.append(f"READBACK_MISMATCH: {mismatched}")
        published = {e.get("name"): e.get("published_git_sha") for e in entries if e.get("kind") == "Agent"}
        stale = [name for name in agents if published.get(name) != sha]
        if stale:
            reasons.append(f"NOT_PUBLISHED_AT_RELEASE: {stale} are not published at {sha}")
    previous_records = {e.get("name"): e.get("published_git_sha") for e in (captured or []) if e.get("kind") == "Agent"}
    return {"verdict": "REGISTRY_RECONCILIATION_FAILED" if reasons else "RELEASE_COMPLETE", "git_sha": sha,
            "agents": agents, "previous_published_sha": {name: previous_records.get(name) for name in agents},
            "reasons": reasons}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("--release-verdict", type=Path, required=True)
    rec.add_argument("--before", type=Path, required=True)
    rec.add_argument("--after", type=Path, required=True)
    rec.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    def load(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    result = reconcile(load(args.release_verdict) or {}, load(args.before), load(args.after))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "reasons": result["reasons"]}, sort_keys=True))
    return 0 if result["verdict"] == "RELEASE_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
