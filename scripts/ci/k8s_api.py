"""Minimal Kubernetes API client for Jenkins CD (stdlib only, no kubectl).

Jenkins authenticates with its projected ServiceAccount token, so every call is
checked against the namespace-scoped Roles in infra/kubernetes/jenkins/rbac/.
Only the kinds the bounded CD workloads need are supported, and every object
must name a namespace from the caller's allowlist. For a local dry run, point
``--api`` at ``kubectl proxy`` instead of the in-cluster endpoint.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml


SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
FIELD_MANAGER = "ewallet-jenkins-cd"
KINDS: dict[str, tuple[str, str]] = {
    "ConfigMap": ("api/v1", "configmaps"),
    "Service": ("api/v1", "services"),
    "Pod": ("api/v1", "pods"),
    "Deployment": ("apis/apps/v1", "deployments"),
    "StatefulSet": ("apis/apps/v1", "statefulsets"),
    "Job": ("apis/batch/v1", "jobs"),
    "CronJob": ("apis/batch/v1", "cronjobs"),
}


class KubeError(RuntimeError):
    """A Kubernetes call failed or a CD policy check rejected the request."""


@dataclass(frozen=True)
class Client:
    api: str
    token_file: Path | None = None
    ca_file: Path | None = None
    opener: Callable[..., Any] = urlopen

    @classmethod
    def from_environment(cls, api: str | None) -> "Client":
        if api:
            return cls(api.rstrip("/"))
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host or not (SA_DIR / "token").is_file():
            raise KubeError("no in-cluster ServiceAccount token; pass --api for a local proxy")
        return cls(f"https://{host}:{port}", SA_DIR / "token", SA_DIR / "ca.crt")

    def request(self, method: str, path: str, body: Any = None, content_type: str = "application/json",
                raw: bool = False) -> Any:
        headers = {"Accept": "application/json"}
        if self.token_file:
            # Re-read every call: the projected token rotates before it expires.
            headers["Authorization"] = "Bearer " + self.token_file.read_text().strip()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        context = ssl.create_default_context(cafile=str(self.ca_file)) if self.ca_file else None
        request = Request(f"{self.api}/{path.lstrip('/')}", data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=60, context=context) as response:
                payload = response.read()
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise KubeError(f"{method} {path} -> HTTP {error.code}: {detail}") from error
        if raw:
            return payload.decode(errors="replace")
        return json.loads(payload) if payload else {}


def _path(kind: str, namespace: str, name: str | None = None) -> str:
    if kind not in KINDS:
        raise KubeError(f"kind {kind} is not supported by the CD client")
    prefix, plural = KINDS[kind]
    path = f"{prefix}/namespaces/{quote(namespace)}/{plural}"
    return f"{path}/{quote(name)}" if name else path


def load_documents(paths: list[Path], allowed_namespaces: set[str]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not document:
                continue
            namespace = document.get("metadata", {}).get("namespace")
            if namespace not in allowed_namespaces:
                raise KubeError(
                    f"{path}: {document.get('kind')}/{document.get('metadata', {}).get('name')} "
                    f"namespace {namespace!r} is outside {sorted(allowed_namespaces)}"
                )
            _path(document.get("kind", ""), namespace)
            documents.append(document)
    if not documents:
        raise KubeError("no Kubernetes documents to apply")
    return documents


def apply(client: Client, documents: list[dict[str, Any]]) -> list[str]:
    applied = []
    query = urlencode({"fieldManager": FIELD_MANAGER, "force": "true"})
    for document in documents:
        metadata = document["metadata"]
        client.request(
            "PATCH", f"{_path(document['kind'], metadata['namespace'], metadata['name'])}?{query}",
            document, content_type="application/apply-patch+yaml",
        )
        applied.append(f"{document['kind']}/{metadata['namespace']}/{metadata['name']}")
    return applied


def get(client: Client, kind: str, namespace: str, name: str) -> dict[str, Any]:
    return client.request("GET", _path(kind, namespace, name))


def delete(client: Client, kind: str, namespace: str, name: str) -> None:
    try:
        client.request("DELETE", _path(kind, namespace, name), {"propagationPolicy": "Background"})
    except KubeError as error:
        if "HTTP 404" not in str(error):
            raise


def job_from_cronjob(client: Client, namespace: str, cronjob: str, job: str,
                     labels: dict[str, str]) -> dict[str, Any]:
    template = get(client, "CronJob", namespace, cronjob)["spec"]["jobTemplate"]
    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": namespace,
                     "labels": {**template.get("metadata", {}).get("labels", {}), **labels},
                     "annotations": {"cronjob.kubernetes.io/instantiate": "manual"}},
        "spec": template["spec"],
    }
    return client.request("POST", _path("Job", namespace), body)


def wait_job(client: Client, namespace: str, name: str, timeout: float,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        status = get(client, "Job", namespace, name).get("status", {})
        for condition in status.get("conditions", []):
            if condition.get("status") == "True" and condition.get("type") in {"Complete", "Failed"}:
                return {"result": condition["type"], "succeeded": status.get("succeeded", 0),
                        "failed": status.get("failed", 0)}
        if time.monotonic() >= deadline:
            return {"result": "Timeout", "succeeded": status.get("succeeded", 0),
                    "failed": status.get("failed", 0)}
        sleep(5)


def wait_ready(client: Client, kind: str, namespace: str, name: str, timeout: float,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        item = get(client, kind, namespace, name)
        wanted = item.get("spec", {}).get("replicas", 1)
        status = item.get("status", {})
        if (status.get("observedGeneration", 0) >= item["metadata"].get("generation", 0)
                and status.get("readyReplicas", 0) >= wanted
                and status.get("updatedReplicas", wanted) >= wanted):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(5)


def job_logs(client: Client, namespace: str, job: str) -> str:
    pods = client.request(
        "GET", f"{_path('Pod', namespace)}?{urlencode({'labelSelector': f'job-name={job}'})}"
    ).get("items", [])
    chunks = []
    for pod in sorted(pods, key=lambda item: item["metadata"]["name"]):
        name = pod["metadata"]["name"]
        for container in pod["spec"]["containers"]:
            text = client.request(
                "GET", f"{_path('Pod', namespace, name)}/log?{urlencode({'container': container['name']})}",
                raw=True,
            )
            chunks.append(f"=== pod/{name} container/{container['name']}\n{text}")
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="Local kubectl proxy URL; default is in-cluster")
    parser.add_argument("--allow-namespace", action="append", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    applied = sub.add_parser("apply")
    applied.add_argument("-f", "--file", type=Path, action="append", required=True)
    for name in ("wait-job", "logs"):
        command = sub.add_parser(name)
        command.add_argument("--namespace", required=True)
        command.add_argument("--job", required=True)
        if name == "wait-job":
            command.add_argument("--timeout", type=float, default=900)
    removed = sub.add_parser("delete")
    removed.add_argument("--namespace", required=True)
    removed.add_argument("--kind", choices=sorted(KINDS), required=True)
    removed.add_argument("--name", required=True)
    fetched = sub.add_parser("get")
    fetched.add_argument("--namespace", required=True)
    fetched.add_argument("--kind", choices=sorted(KINDS), required=True)
    fetched.add_argument("--name", required=True)
    created = sub.add_parser("job-from-cronjob")
    created.add_argument("--namespace", required=True)
    created.add_argument("--cronjob", required=True)
    created.add_argument("--job", required=True)
    created.add_argument("--label", action="append", default=[])
    ready = sub.add_parser("wait-ready")
    ready.add_argument("--namespace", required=True)
    ready.add_argument("--kind", choices=("Deployment", "StatefulSet"), required=True)
    ready.add_argument("--name", required=True)
    ready.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    allowed = set(args.allow_namespace)
    namespace = getattr(args, "namespace", None)
    if namespace is not None and namespace not in allowed:
        parser.error(f"namespace {namespace} is outside the allowlist")
    try:
        client = Client.from_environment(args.api)
        if args.command == "apply":
            print("\n".join(apply(client, load_documents(args.file, allowed))))
        elif args.command == "job-from-cronjob":
            labels = dict(item.split("=", 1) for item in args.label)
            job_from_cronjob(client, args.namespace, args.cronjob, args.job, labels)
            print(f"Job/{args.namespace}/{args.job} created from CronJob/{args.cronjob}")
        elif args.command == "wait-job":
            outcome = wait_job(client, args.namespace, args.job, args.timeout)
            print(json.dumps(outcome, sort_keys=True))
            return 0 if outcome["result"] == "Complete" else 1
        elif args.command == "wait-ready":
            return 0 if wait_ready(client, args.kind, args.namespace, args.name, args.timeout) else 1
        elif args.command == "logs":
            print(job_logs(client, args.namespace, args.job))
        elif args.command == "get":
            print(json.dumps(get(client, args.kind, args.namespace, args.name), sort_keys=True))
        else:
            delete(client, args.kind, args.namespace, args.name)
    except KubeError as error:
        print(f"k8s_api: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
