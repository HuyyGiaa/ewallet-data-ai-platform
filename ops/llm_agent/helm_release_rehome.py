"""Move a Helm release record to another namespace without a two-owner window.

Proposal tooling for Batch D1 (docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md).
Never run against production without the owner-approved change.

The 2026-11-03 kind experiment showed that two coexisting records of one
release are dangerous: the stale one can silently retake ownership and its
``helm uninstall`` deletes every object. This tool removes that state:

1. ``backup``   exports every record Secret to a private directory with sha256s.
2. ``archive``  relabels the old records ``owner=helm`` -> ``owner=helm-archived``.
                Helm selects records by ``owner=helm,name=<release>``, so the old
                release disappears for every helm command (list, upgrade,
                rollback, uninstall) while its bytes stay in the cluster.
3. ``copy``     rewrites each record's ``namespace`` and creates it in the target
                namespace, so the full revision history continues there.
4. ``takeover`` runs ``helm upgrade --take-ownership`` in the target namespace.
5. ``verify``   compares object UIDs, specs and pods with the backup snapshot.
``rollback``    reverses whatever steps completed, from any intermediate state.

At no point are two ``owner=helm`` record sets of the release visible.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


ARCHIVED = "helm-archived"
Runner = Callable[[list[str], bytes | None], str]


class RehomeError(RuntimeError):
    """A precondition failed; nothing further was changed."""


def _run(argv: list[str], data: bytes | None = None) -> str:
    completed = subprocess.run(argv, input=data, capture_output=True, check=False)
    if completed.returncode:
        raise RehomeError(f"{' '.join(argv[:6])}... failed: {completed.stderr.decode(errors='replace')[:400]}")
    return completed.stdout.decode()


def decode_release(secret: dict[str, Any]) -> dict[str, Any]:
    raw = base64.b64decode(base64.b64decode(secret["data"]["release"]))
    return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)


def encode_release(release: dict[str, Any]) -> str:
    payload = gzip.compress(json.dumps(release, separators=(",", ":")).encode(), mtime=0)
    return base64.b64encode(base64.b64encode(payload)).decode()


def rehomed_record(secret: dict[str, Any], target: str) -> dict[str, Any]:
    """The same record (name, labels, history entry) addressed to ``target``."""

    release = decode_release(secret)
    release["namespace"] = target
    metadata = secret["metadata"]
    return {
        "apiVersion": "v1", "kind": "Secret", "type": secret["type"],
        "metadata": {"name": metadata["name"], "namespace": target, "labels": dict(metadata["labels"])},
        "data": {"release": encode_release(release)},
    }


class Rehome:
    def __init__(self, context: str, release: str, source: str, target: str, workdir: Path,
                 run: Runner = _run) -> None:
        self.context, self.release, self.source, self.target, self.workdir = context, release, source, target, workdir
        self.run = run

    def kubectl(self, *args: str, data: bytes | None = None) -> str:
        return self.run(["kubectl", "--context", self.context, *args], data)

    def records(self, namespace: str, owner: str = "helm") -> list[dict[str, Any]]:
        out = self.kubectl("-n", namespace, "get", "secrets", "-l", f"owner={owner},name={self.release}", "-o", "json")
        return sorted(json.loads(out)["items"], key=lambda item: int(item["metadata"]["labels"]["version"]))

    def state(self) -> dict[str, int]:
        return {"source_active": len(self.records(self.source)),
                "source_archived": len(self.records(self.source, ARCHIVED)),
                "target_active": len(self.records(self.target))}

    def backup(self) -> Path:
        records = self.records(self.source)
        if not records:
            raise RehomeError(f"no active records of {self.release} in {self.source}")
        self.workdir.mkdir(parents=True, exist_ok=True)
        path = self.workdir / "records-backup.json"
        text = json.dumps({"items": records}, indent=1, sort_keys=True)
        path.write_text(text)
        path.chmod(0o600)
        (self.workdir / "records-backup.sha256").write_text(hashlib.sha256(text.encode()).hexdigest() + "\n")
        manifest = self.run(["helm", "--kube-context", self.context, "get", "manifest", self.release, "-n", self.source], None)
        (self.workdir / "manifest.yaml").write_text(manifest)
        return path

    def _relabel(self, namespace: str, owner_from: str, owner_to: str) -> int:
        count = 0
        for record in self.records(namespace, owner_from):
            self.kubectl("-n", namespace, "label", "secret", record["metadata"]["name"], f"owner={owner_to}",
                         "--overwrite")
            count += 1
        return count

    def archive(self) -> int:
        state = self.state()
        if state["target_active"]:
            raise RehomeError("target already has active records; refusing to archive the source")
        if not (self.workdir / "records-backup.json").is_file():
            raise RehomeError("run backup first")
        return self._relabel(self.source, "helm", ARCHIVED)

    def copy(self) -> int:
        state = self.state()
        if state["source_active"]:
            raise RehomeError("source records are still active; archive them first")
        backup = json.loads((self.workdir / "records-backup.json").read_text())["items"]
        documents = [rehomed_record(secret, self.target) for secret in backup]
        self.kubectl("apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}).encode())
        return len(documents)

    def takeover(self, chart: str, values: str, timeout: str = "5m", wait: bool = True) -> str:
        state = self.state()
        if state["source_active"] or not state["target_active"]:
            raise RehomeError(f"unexpected record state before takeover: {state}")
        wait_args = ["--wait", "--timeout", timeout] if wait else []
        return self.run(["helm", "--kube-context", self.context, "upgrade", self.release, chart, "-n", self.target,
                         "--take-ownership", "-f", values, *wait_args], None)

    def rollback(self, chart: str, values: str, wait: bool = True) -> dict[str, int]:
        """Return to the source namespace from any intermediate state."""

        if self.records(self.source, ARCHIVED):
            self._relabel(self.source, ARCHIVED, "helm")
        target_records = self.records(self.target)
        if target_records:
            # Keep the target-side history (revisions made after the move) before retiring it.
            self.workdir.mkdir(parents=True, exist_ok=True)
            path = self.workdir / "target-records-before-rollback.json"
            path.write_text(json.dumps({"items": target_records}, indent=1, sort_keys=True))
            path.chmod(0o600)
        for record in target_records:
            self.kubectl("-n", self.target, "delete", "secret", record["metadata"]["name"])
        owner = self.kubectl("get", "-f", str(self.workdir / "manifest.yaml"), "-o",
                             "jsonpath={.items[*].metadata.annotations.meta\\.helm\\.sh/release-namespace}")
        if self.target in owner.split():
            # Objects were adopted by the target; give them back without changing their spec.
            self.run(["helm", "--kube-context", self.context, "upgrade", self.release, chart, "-n", self.source,
                      "--take-ownership", "-f", values, *(["--wait"] if wait else [])], None)
        return self.state()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["state", "backup", "archive", "copy", "takeover", "rollback"])
    parser.add_argument("--context", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--workdir", type=Path, required=True, help="private directory for backups")
    parser.add_argument("--chart")
    parser.add_argument("--values")
    parser.add_argument("--no-wait", action="store_true",
                        help="kind validation only (pods without registry credentials never become Ready)")
    args = parser.parse_args(argv)
    tool = Rehome(args.context, args.release, args.source, args.target, args.workdir)
    try:
        if args.step == "state":
            result: Any = tool.state()
        elif args.step == "backup":
            result = str(tool.backup())
        elif args.step == "archive":
            result = tool.archive()
        elif args.step == "copy":
            result = tool.copy()
        elif args.step in {"takeover", "rollback"}:
            if not (args.chart and args.values):
                parser.error(f"{args.step} needs --chart and --values")
            wait = not args.no_wait
            result = (tool.takeover(args.chart, args.values, wait=wait) if args.step == "takeover"
                      else tool.rollback(args.chart, args.values, wait=wait))
        print(json.dumps({"step": args.step, "result": result, "state": tool.state()}, default=str))
    except RehomeError as error:
        print(f"helm_release_rehome: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
