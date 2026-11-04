from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from scripts.ci.k8s_api import (
    Client, KubeError, apply, delete, job_from_cronjob, job_logs, load_documents, wait_job,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return False


class FakeApi:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], object]] = []

    def __call__(self, request, timeout, context):  # type: ignore[no-untyped-def]
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, request.full_url, dict(request.header_items()), body))
        response = self.responses.pop(0)
        if isinstance(response, HTTPError):
            raise response
        payload = response if isinstance(response, (bytes, str)) else json.dumps(response)
        return FakeResponse(payload.encode() if isinstance(payload, str) else payload)


class K8sApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, text: str) -> Path:
        path = self.root / "m.yaml"
        path.write_text(text)
        return path

    def test_documents_outside_allowlist_are_rejected_before_any_call(self) -> None:
        path = self.write("kind: ConfigMap\nmetadata: {name: x, namespace: ewallet-mcp}\n")
        with self.assertRaisesRegex(KubeError, "outside"):
            load_documents([path], {"ewallet-cicd"})

    def test_unsupported_kind_is_rejected(self) -> None:
        path = self.write("kind: Secret\nmetadata: {name: x, namespace: ewallet-cicd}\n")
        with self.assertRaisesRegex(KubeError, "not supported"):
            load_documents([path], {"ewallet-cicd"})

    def test_apply_uses_server_side_apply_with_fixed_field_manager_and_fresh_token(self) -> None:
        token = self.root / "token"
        token.write_text("t1\n")
        api = FakeApi([{}, {}])
        client = Client("https://k8s", token_file=token, opener=api)
        docs = load_documents([self.write(
            "apiVersion: batch/v1\nkind: CronJob\nmetadata: {name: rag, namespace: ewallet-cicd}\n---\n"
            "apiVersion: v1\nkind: Service\nmetadata: {name: redis, namespace: ewallet-cicd}\n"
        )], {"ewallet-cicd"})
        applied = apply(client, docs[:1])
        token.write_text("t2\n")
        apply(client, docs[1:])
        self.assertEqual(applied, ["CronJob/ewallet-cicd/rag"])
        method, url, headers, _ = api.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(
            url,
            "https://k8s/apis/batch/v1/namespaces/ewallet-cicd/cronjobs/rag"
            "?fieldManager=ewallet-jenkins-cd&force=true",
        )
        self.assertEqual(headers["Content-type"], "application/apply-patch+yaml")
        self.assertEqual(headers["Authorization"], "Bearer t1")
        self.assertEqual(api.calls[1][2]["Authorization"], "Bearer t2")

    def test_job_from_cronjob_copies_template_and_marks_manual_run(self) -> None:
        api = FakeApi([
            {"spec": {"jobTemplate": {"metadata": {"labels": {"app": "rag"}},
                                      "spec": {"template": {"spec": {"containers": []}}}}}},
            {},
        ])
        job_from_cronjob(Client("http://p", opener=api), "ewallet-cicd", "rag", "rag-smoke",
                         {"ewallet.io/git-sha": "a" * 40})
        method, url, _, body = api.calls[1]
        self.assertEqual((method, url), ("POST", "http://p/apis/batch/v1/namespaces/ewallet-cicd/jobs"))
        self.assertEqual(body["metadata"]["labels"], {"app": "rag", "ewallet.io/git-sha": "a" * 40})
        self.assertEqual(body["metadata"]["annotations"], {"cronjob.kubernetes.io/instantiate": "manual"})

    def test_wait_job_reports_failed_and_timeout(self) -> None:
        failed = FakeApi([{"status": {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}}])
        self.assertEqual(wait_job(Client("http://p", opener=failed), "ns", "j", 10)["result"], "Failed")
        running = FakeApi([{"status": {}}])
        self.assertEqual(
            wait_job(Client("http://p", opener=running), "ns", "j", 0, sleep=lambda _: None)["result"],
            "Timeout",
        )

    def test_job_logs_reads_every_container_of_the_job_pods(self) -> None:
        api = FakeApi([
            {"items": [{"metadata": {"name": "p1"}, "spec": {"containers": [{"name": "a"}, {"name": "b"}]}}]},
            "log-a", "log-b",
        ])
        text = job_logs(Client("http://p", opener=api), "ns", "j")
        self.assertIn("labelSelector=job-name%3Dj", api.calls[0][1])
        self.assertIn("container/a\nlog-a", text)
        self.assertIn("container/b\nlog-b", text)

    def test_delete_ignores_missing_objects_but_raises_other_errors(self) -> None:
        missing = HTTPError("u", 404, "nf", {}, io.BytesIO(b"{}"))
        delete(Client("http://p", opener=FakeApi([missing])), "Job", "ns", "j")
        forbidden = HTTPError("u", 403, "no", {}, io.BytesIO(b"forbidden"))
        with self.assertRaisesRegex(KubeError, "HTTP 403"):
            delete(Client("http://p", opener=FakeApi([forbidden])), "Job", "ns", "j")


if __name__ == "__main__":
    unittest.main()
