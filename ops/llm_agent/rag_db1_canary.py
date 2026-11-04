"""Render and verify the short-lived RAG DB1 production-equivalent MCP canary.

The renderer deliberately excludes Secret values and the immutable registry
payload. Operators create those two inputs from read-back sources, apply this
manifest, run ``call``, then delete every resource carrying the ownership label.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import yaml


NAME = "rag-db1-canary"
NAMESPACE = "ewallet-mcp"
OWNER_LABEL = "edai2-rag-db1-canary"
DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CanaryConfig:
    image: str
    git_sha: str
    registry_config_map: str
    redis_secret: str
    auth_secret: str
    project: str
    run_id: str
    corpus_revision: str
    feature_revision: str

    def __post_init__(self) -> None:
        if not DIGEST_IMAGE.fullmatch(self.image):
            raise ValueError("image must use an immutable sha256 digest")
        if not SHA.fullmatch(self.git_sha):
            raise ValueError("git_sha must be a full 40-character lowercase SHA")
        for field in ("registry_config_map", "redis_secret", "auth_secret", "project", "run_id",
                      "corpus_revision", "feature_revision"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} is required")


def _env(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def render_resources(config: CanaryConfig) -> list[dict[str, Any]]:
    labels = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "mcp-server-canary",
              "ewallet.io/owned-by": OWNER_LABEL}
    env: list[dict[str, Any]] = [
        _env("EWALLET_GIT_SHA", config.git_sha),
        _env("EWALLET_IMAGE_DIGEST", config.image.rsplit("@", 1)[1]),
        _env("EWALLET_CONFIG_REVISION", f"rag-db1-canary/{config.run_id}"),
        _env("EWALLET_PROMPT_REVISION", "v2"),
        _env("EWALLET_MODEL_REVISION", "fraud-detector/1"),
        _env("EWALLET_FEATURE_REVISION", config.feature_revision),
        _env("EWALLET_CORPUS_REVISION", config.corpus_revision),
        _env("EWALLET_TOOL_MAX_CONCURRENCY", "2"),
        _env("EWALLET_TOOL_MAX_TIMEOUT_MS", "20000"),
        _env("EWALLET_MCP_ALLOWED_ORIGINS", ""),
        _env("EWALLET_MCP_MAX_BODY_BYTES", "65536"),
        _env("EWALLET_MCP_MAX_INFLIGHT", "4"),
        _env("EWALLET_MCP_BODY_TIMEOUT_SECONDS", "10"),
        _env("EWALLET_MCP_TOOL_GRANTS", json.dumps({
            "drift-specialist": ["feature_lookup"],
            "feature-rag-specialist": ["feature_lookup", "fraud_scoring", "knowledge_search"],
        }, separators=(",", ":"))),
        _env("EWALLET_MCP_DISCOVERY_SUBJECTS", '["kagent-controller"]'),
        {"name": "EWALLET_REDIS_CONNECTION_STRING", "valueFrom": {
            "secretKeyRef": {"name": config.redis_secret, "key": "connection_string"}}},
        {"name": "EWALLET_MCP_SERVICE_TOKENS", "valueFrom": {
            "secretKeyRef": {"name": config.auth_secret, "key": "auth-config.json"}}},
        _env("EWALLET_FEAST_REGISTRY", "/feast/fraud/registry.db"),
        _env("EWALLET_FEAST_REPO_PATH", "/workspace/ai_platform/features/feast/online_repo"),
        _env("EWALLET_KSERVE_BASE_URL", "http://fraud-detector-predictor.fraud-serving.svc.cluster.local"),
        _env("EWALLET_MODEL_NAME", "fraud-detector"),
        _env("EWALLET_MODEL_VERSION", "1"),
        _env("EWALLET_RAG_EMBEDDER", "local-onnx"),
        _env("EWALLET_RAG_FEAST_PROJECT", config.project),
        _env("EWALLET_RAG_FEAST_REGISTRY", "/feast/rag/registry.db"),
        _env("EWALLET_RAG_MANIFEST", "/workspace/data/llm_agent/rag/fraud-policy-v1/manifest.json"),
        _env("EWALLET_RAG_MODEL_CACHE", "/opt/hf-cache"),
        _env("EWALLET_RAG_ONLINE_STORE", "redis"),
        _env("EWALLET_RAG_RETRIEVER", "feast-online-exact"),
    ]
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
                 "strategy": {"type": "Recreate"}, "template": {
            "metadata": {"labels": labels},
            "spec": {"serviceAccountName": "feature-rag-mcp", "automountServiceAccountToken": False,
                     "nodeSelector": {"ewallet.io/g3-llm": "true"},
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{
                         "name": "mcp-server", "image": config.image, "imagePullPolicy": "IfNotPresent",
                         "command": ["python", "-m", "uvicorn", "--factory",
                                     "ai_platform.llm_agent.tools.service:create_feature_rag_app",
                                     "--host", "0.0.0.0", "--port", "8080", "--no-access-log"],
                         "env": env, "ports": [{"name": "http", "containerPort": 8080}],
                         "startupProbe": {"httpGet": {"path": "/health/live", "port": "http"},
                                          "failureThreshold": 30, "periodSeconds": 2},
                         "livenessProbe": {"httpGet": {"path": "/health/live", "port": "http"},
                                           "periodSeconds": 15},
                         "readinessProbe": {"httpGet": {"path": "/health/ready", "port": "http"},
                                            "periodSeconds": 10, "timeoutSeconds": 5},
                         "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                       "limits": {"cpu": "500m", "memory": "512Mi"}},
                         "securityContext": {"allowPrivilegeEscalation": False,
                                             "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]},
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"},
                                          {"name": "fraud-registry", "mountPath": "/feast/fraud",
                                           "readOnly": True},
                                          {"name": "rag-registry", "mountPath": "/feast/rag",
                                           "readOnly": True}],
                     }],
                     "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
                                 {"name": "fraud-registry", "configMap": {
                                     "name": "feast-registry-fraud-online"}},
                                 {"name": "rag-registry", "configMap": {
                                     "name": config.registry_config_map}}]}}}}
    probe = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
             "metadata": {"name": "allow-ambient-probes-rag-db1-canary", "namespace": NAMESPACE,
                          "labels": labels},
             "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
                      "policyTypes": ["Ingress"], "ingress": [{"from": [{"ipBlock": {
                          "cidr": "169.254.7.127/32"}}], "ports": [{"protocol": "TCP", "port": 8080}]}]}}
    egress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "allow-rag-db1-canary-to-feast-redis", "namespace": NAMESPACE,
                           "labels": labels},
              "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
                       "policyTypes": ["Egress"], "egress": [{"to": [{"namespaceSelector": {
                           "matchLabels": {"kubernetes.io/metadata.name": "feature-store"}},
                           "podSelector": {"matchLabels": {"app.kubernetes.io/name": "redis"}}}],
                           "ports": [{"protocol": "TCP", "port": 6379}]}]}}
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": "allow-ewallet-mcp-rag-db1-canary-to-feast-redis",
                            "namespace": "feature-store", "labels": labels},
               "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "redis"}},
                        "policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                            "podSelector": {"matchLabels": {"app.kubernetes.io/name": NAME}}}],
                            "ports": [{"protocol": "TCP", "port": 6379}]}]}}
    return [deployment, probe, egress, ingress]


def derive_redis_database(connection_string: str, database: int) -> str:
    value = connection_string.strip()
    if not 0 <= database <= 15:
        raise ValueError("expected Redis database 0..15")
    parsed = urlsplit(value)
    if parsed.scheme in {"redis", "rediss"} and parsed.hostname:
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))
    # Feast also accepts ``host:port,password=...,db=N``. Preserve every
    # credential/timeout segment byte-for-byte and only replace/add ``db``.
    segments = value.split(",")
    if not segments or not re.fullmatch(r"[^\s,:/]+:\d{1,5}", segments[0]):
        raise ValueError("expected a Redis URL or Feast host:port connection string")
    updated: list[str] = [segments[0]]
    seen = False
    for segment in segments[1:]:
        if segment.startswith("db="):
            if seen:
                raise ValueError("Redis connection contains duplicate db settings")
            updated.append(f"db={database}")
            seen = True
        else:
            updated.append(segment)
    if not seen:
        updated.append(f"db={database}")
    return ",".join(updated)


def verify_read_after_write(*, response: Mapping[str, Any], documents: Mapping[str, Any],
                            chunks: Mapping[str, Any], publication: Mapping[str, Any],
                            redis_readback: Mapping[str, Any], registry_sha256: str) -> dict[str, Any]:
    structured = response.get("structured_content") or response.get("structuredContent")
    if not isinstance(structured, Mapping) or response.get("is_error") is True:
        raise ValueError("MCP response is missing successful structured content")
    if structured.get("tool_name") != "knowledge_search" or structured.get("status") != "ok":
        raise ValueError("MCP knowledge_search did not return status=ok")
    expected_chunks = {row["chunk_id"]: row for row in chunks["chunks"]}
    citations = structured.get("citations") or []
    if not citations:
        raise ValueError("MCP response has no citations")
    for citation in citations:
        expected = expected_chunks.get(citation.get("chunk_id"))
        if expected is None:
            raise ValueError("citation references a chunk outside the Airflow publication")
        observed = (citation.get("document_id"), citation.get("source_uri"),
                    str(citation.get("source_checksum", "")).removeprefix("sha256:"),
                    citation.get("corpus_revision"), citation.get("feature_revision"))
        wanted = (expected["document_id"], expected["source_uri"], expected["source_checksum"],
                  expected["corpus_revision"], publication["feature_index_revision"])
        if observed != wanted or not citation.get("embedding_revision"):
            raise ValueError(f"citation integrity mismatch for {citation.get('chunk_id')}")
    if documents.get("run_id") != publication.get("run_id") or publication.get("row_count") != len(expected_chunks):
        raise ValueError("Airflow publication identity is internally inconsistent")
    if redis_readback.get("db") != 1 or redis_readback.get("matching_keys") != publication.get("row_count"):
        raise ValueError("Redis DB1 readback does not match the immutable publication")
    if not re.fullmatch(r"[0-9a-f]{64}", registry_sha256):
        raise ValueError("registry_sha256 must be a lowercase SHA-256")
    return {
        "verdict": "MCP_CANARY_READ_AFTER_WRITE_PASS",
        "isolated_readback": "PASS", "production_db0_cutover": "NOT_PERFORMED",
        "airflow_run_id": publication["run_id"], "publication_identity": publication["publication_identity"],
        "registry": publication["feast"]["registry"], "registry_sha256": registry_sha256,
        "redis_db": 1, "redis_matching_keys": redis_readback["matching_keys"],
        "verified_citations": len(citations),
        "document_ids": sorted({citation["document_id"] for citation in citations}),
        "chunk_ids": sorted(citation["chunk_id"] for citation in citations),
    }


def mcp_headers(token: str) -> dict[str, str]:
    """Return the production service-token header; never use generic Bearer auth."""
    return {"x-ewallet-mcp-service-token": token}


async def _call(url: str, token: str, query: str, top_k: int) -> dict[str, Any]:
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = mcp_headers(token)
    async with httpx2.AsyncClient(headers=headers, timeout=30) as client:
        async with streamable_http_client(url, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("knowledge_search", {"query": query, "top_k": top_k})
    return {"transport": "official-mcp-sdk-streamable-http", "protocol_version": initialized.protocol_version,
            "server": initialized.server_info.name, "tools": sorted(tool.name for tool in tools.tools),
            "tool": "knowledge_search", "is_error": bool(result.is_error),
            "structured_content": result.structured_content}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    for argument in ("image", "git-sha", "registry-config-map", "redis-secret", "auth-secret", "project",
                     "run-id", "corpus-revision", "feature-revision"):
        render.add_argument(f"--{argument}", required=True)
    call = sub.add_parser("call")
    call.add_argument("--url", required=True)
    call.add_argument("--token-env", default="EWALLET_MCP_TOKEN")
    call.add_argument("--query", required=True)
    call.add_argument("--top-k", type=int, default=5, choices=range(1, 6))
    args = parser.parse_args(argv)
    if args.command == "render":
        config = CanaryConfig(image=args.image, git_sha=args.git_sha,
                              registry_config_map=args.registry_config_map, redis_secret=args.redis_secret,
                              auth_secret=args.auth_secret, project=args.project, run_id=args.run_id,
                              corpus_revision=args.corpus_revision, feature_revision=args.feature_revision)
        print(yaml.safe_dump_all(render_resources(config), sort_keys=False), end="")
        return 0
    token = os.environ.get(args.token_env, "")
    if not token:
        raise ValueError(f"{args.token_env} is required and is never accepted as a command-line argument")
    print(json.dumps(asyncio.run(_call(args.url, token, args.query, args.top_k)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
