from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from ops.llm_agent.helm_release_rehome import Rehome, RehomeError, decode_release, rehomed_record


def record(version: int, namespace: str = "ewallet-mcp", owner: str = "helm") -> dict:
    release = {"name": "llm-agent-mcp", "namespace": namespace, "version": version, "manifest": "kind: X\n"}
    data = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(release).encode()))).decode()
    return {"type": "helm.sh/release.v1", "data": {"release": data},
            "metadata": {"name": f"sh.helm.release.v1.llm-agent-mcp.v{version}", "namespace": namespace,
                         "labels": {"owner": owner, "name": "llm-agent-mcp", "status": "superseded",
                                    "version": str(version)}}}


class FakeCluster:
    def __init__(self, items: dict[tuple[str, str], list[dict]]) -> None:
        self.items, self.calls = items, []

    def __call__(self, argv: list[str], data: bytes | None) -> str:
        self.calls.append(argv)
        if "get" in argv and "secrets" in argv:
            namespace = argv[argv.index("-n") + 1]
            owner = argv[argv.index("-l") + 1].split(",")[0].split("=")[1]
            return json.dumps({"items": self.items.get((namespace, owner), [])})
        if argv[:1] == ["helm"]:
            return "manifest\n"
        return ""


class RehomeTests(unittest.TestCase):
    def test_rehomed_record_keeps_identity_and_history_and_changes_only_namespace(self) -> None:
        original = record(6)
        moved = rehomed_record(original, "ewallet-mcp-release")
        self.assertEqual(moved["metadata"]["name"], original["metadata"]["name"])
        self.assertEqual(moved["metadata"]["labels"], original["metadata"]["labels"])
        before, after = decode_release(original), decode_release(moved)
        self.assertEqual(after["namespace"], "ewallet-mcp-release")
        self.assertEqual({**after, "namespace": before["namespace"]}, before)

    def test_archive_refuses_when_target_is_already_active_or_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            busy = Rehome("ctx", "llm-agent-mcp", "ewallet-mcp", "ewallet-mcp-release", Path(tmp),
                          run=FakeCluster({("ewallet-mcp-release", "helm"): [record(1, "ewallet-mcp-release")],
                                           ("ewallet-mcp", "helm"): [record(6)]}))
            with self.assertRaisesRegex(RehomeError, "target already has active records"):
                busy.archive()
            fresh = Rehome("ctx", "llm-agent-mcp", "ewallet-mcp", "ewallet-mcp-release", Path(tmp),
                           run=FakeCluster({("ewallet-mcp", "helm"): [record(6)]}))
            with self.assertRaisesRegex(RehomeError, "run backup first"):
                fresh.archive()

    def test_copy_and_takeover_refuse_while_source_records_are_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Rehome("ctx", "llm-agent-mcp", "ewallet-mcp", "ewallet-mcp-release", Path(tmp),
                          run=FakeCluster({("ewallet-mcp", "helm"): [record(6)]}))
            with self.assertRaisesRegex(RehomeError, "archive them first"):
                tool.copy()
            with self.assertRaisesRegex(RehomeError, "unexpected record state"):
                tool.takeover("chart", "values.yaml")

    def test_backup_is_private_and_checksummed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Rehome("ctx", "llm-agent-mcp", "ewallet-mcp", "ewallet-mcp-release", Path(tmp) / "b",
                          run=FakeCluster({("ewallet-mcp", "helm"): [record(5), record(6)]}))
            path = tool.backup()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(json.loads(path.read_text())["items"]), 2)
            self.assertTrue((Path(tmp) / "b/records-backup.sha256").is_file())


class RollbackHistoryTests(unittest.TestCase):
    def test_rollback_backs_up_target_history_before_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cluster = FakeCluster({("ewallet-mcp-release", "helm"): [record(7, "ewallet-mcp-release"),
                                                                     record(8, "ewallet-mcp-release")]})
            tool = Rehome("ctx", "llm-agent-mcp", "ewallet-mcp", "ewallet-mcp-release", Path(tmp), run=cluster)
            (Path(tmp) / "manifest.yaml").write_text("kind: X\n")
            tool.rollback("chart", "values.yaml")
            saved = json.loads((Path(tmp) / "target-records-before-rollback.json").read_text())["items"]
            self.assertEqual([item["metadata"]["labels"]["version"] for item in saved], ["7", "8"])
            deletes = [argv for argv in cluster.calls if "delete" in argv]
            self.assertEqual(len(deletes), 2)


if __name__ == "__main__":
    unittest.main()
