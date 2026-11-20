"""Create (and revoke) the credentials of one Agent staging campaign (CI/CD V2-P3B).

The authoritative Level A inventory is ``SECRETS`` below; jenkins/cd/agent-staging.contract.json and
docs/workplans/edai2/CICD_V2_P3B0_PREREQUISITES.md list the same names. Every value is generated here, in
memory, kept only in the Kubernetes Secrets it creates, and never printed or written to disk. Nothing is copied
from production.

Why a separate tool from ops/llm_agent/g3_mcp_credentials.py: that tool rotates an existing production server
set (transition/final modes need the previous table) inside one client namespace. Staging is a new, isolated set
whose clients live in two namespaces (Specialist actors in ewallet-sandbox-staging, the kagent controller's
discovery token beside the staging MCP Gateway). The names and the auth-table format are the same
(``token_secret_name``, ``auth_secret_name``, ai_platform/llm_agent/tools/security.py version 1).

    python -m ops.llm_agent.staging_credentials plan                       # offline: names, keys, consumers
    python -m ops.llm_agent.staging_credentials create --context CTX [--approve]
    python -m ops.llm_agent.staging_credentials revoke --context CTX [--approve]
    ... create|revoke --profile managed-prompt ...   # the two LLM keys of values-staging-managed-prompt.yaml

``create`` refuses when any of the Secrets already exists (no overwrite, no partial merge); ``revoke`` deletes
exactly these names in the two staging namespaces.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import secrets
import subprocess
import sys
from typing import Callable, Mapping, Sequence

from ops.llm_agent.g3_mcp_credentials import TOKEN_BYTES, auth_secret_name, token_secret_name

AGENTS = "ewallet-agents-staging"
SANDBOX = "ewallet-sandbox-staging"
RELEASE = "staging"   # mcpCredentialLifecycle.serverSet / clientRelease in values-staging.yaml
FEAST_REDIS_HOST = "feast-staging-redis.ewallet-agents-staging.svc.cluster.local:6379"
IDEMPOTENCY_HOST = "coordinator-idempotency-redis.ewallet-agents-staging.svc.cluster.local:6379"
# Tool grants and discovery subjects of the staging MCP servers (values-staging.yaml mcpServices.services).
GRANTS = {"feature-rag-mcp": ("sandbox-feature-rag",), "drift-mcp": ("sandbox-drift",)}
DISCOVERY_SUBJECT = "kagent-controller"
# coordinator_idempotency_runbook.md: the default user, the idempotency key prefix and the Lua state machine's
# commands, plus SELECT/MULTI/EXEC/PEXPIREAT for AOF replay.
IDEMPOTENCY_ACL = ("user default on >{password} ~ewallet:coordinator:idempotency:v1:* resetchannels -@all "
                   "+ping +eval +hget +hset +del +persist +pexpire +pexpireat +time +select +multi +exec")
LABELS = {"app.kubernetes.io/part-of": "ewallet-llm-agent", "ewallet.io/environment": "staging",
          "ewallet.io/credential": "agent-staging"}


@dataclass(frozen=True)
class SecretSpec:
    namespace: str
    name: str
    keys: tuple[str, ...]
    consumer: str
    purpose: str


def _mcp_specs() -> list[SecretSpec]:
    specs = []
    for server, subjects in GRANTS.items():
        specs.append(SecretSpec(AGENTS, auth_secret_name(server, RELEASE), ("auth-config.json",),
                                f"Deployment/{server}", f"{server} token table: {', '.join(subjects)} + {DISCOVERY_SUBJECT}"))
        specs.append(SecretSpec(AGENTS, token_secret_name(DISCOVERY_SUBJECT, server, RELEASE), ("token",),
                                f"RemoteMCPServer/{server} (kagent controller)", "list-only tool discovery"))
        for subject in subjects:
            agent = "feature-rag-specialist" if subject == "sandbox-feature-rag" else "drift-specialist"
            specs.append(SecretSpec(SANDBOX, token_secret_name(subject, server, RELEASE), ("token",),
                                    f"SandboxAgent/{agent}", f"{agent} calls {server} through the MCP Gateway"))
    return specs


SECRETS: tuple[SecretSpec, ...] = tuple(_mcp_specs()) + (
    SecretSpec(AGENTS, "coordinator-idempotency-redis-auth-staging", ("password", "connection_url", "users.acl"),
               "StatefulSet/coordinator-idempotency-redis, Agent/fraud-coordinator", "durable idempotency store"),
    SecretSpec(AGENTS, "feast-staging-redis-auth", ("password",), "Deployment/feast-staging-redis",
               "staging Feast Online Redis server (infra/kubernetes/agent-staging/feast-redis.yaml)"),
    SecretSpec(AGENTS, "feast-online-redis-staging-fraud", ("connection_string",), "Deployment/feature-rag-mcp",
               "Fraud Feast Online target, DB 1"),
    SecretSpec(AGENTS, "feast-online-redis-staging-rag", ("connection_string",), "Deployment/feature-rag-mcp",
               "RAG Feast Online target, DB 4"),
)
# Rendered (the ModelConfig CR names it) but read only by natural-language intake, which Level A keeps off.
# Both are created only for the Managed Prompt profile (values-staging-managed-prompt.yaml): the staging
# agentgateway and the staging vLLM. Fresh values; the production agentgateway/vLLM keys are never read.
LLM_ONLY = (SecretSpec(AGENTS, "agentgateway-client-key-staging", ("api-key",), "ModelConfig/ewallet-staging-llm",
                       "LLM Gateway client key: managed-prompt / NL runtime test only"),
            SecretSpec(AGENTS, "vllm-api-key-staging", ("api-key",), "Deployment/vllm-primary, Deployment/agentgateway",
                       "staging vLLM API key: managed-prompt runtime test only"))
PROFILES = {"level-a": SECRETS, "managed-prompt": LLM_ONLY}


def _token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _documents(specs: Sequence[SecretSpec], values: Mapping[str, Mapping[str, str]]) -> list[dict]:
    documents = []
    for spec in specs:
        data = values[spec.name]
        if tuple(sorted(data)) != tuple(sorted(spec.keys)):
            raise RuntimeError(f"{spec.name} keys {sorted(data)} != {sorted(spec.keys)}")
        documents.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
                          "metadata": {"name": spec.name, "namespace": spec.namespace, "labels": LABELS},
                          "stringData": dict(data)})
    return documents


def llm_manifests(token: Callable[[], str] = _token) -> list[dict]:
    """The Managed Prompt profile's two LLM keys, fresh and distinct (in memory only)."""

    return _documents(LLM_ONLY, {spec.name: {"api-key": token()} for spec in LLM_ONLY})


def manifests(token: Callable[[], str] = _token) -> list[dict]:
    """Every Level A Secret with fresh values (in memory only)."""

    values: dict[str, dict[str, str]] = {}
    for server, subjects in GRANTS.items():
        entries = []
        for subject in (*subjects, DISCOVERY_SUBJECT):
            value = token()
            entries.append({"id": f"{subject}.{server}.{RELEASE}.active", "subject": subject, "token": value,
                            "status": "active"})
            values[token_secret_name(subject, server, RELEASE)] = {"token": value}
        values[auth_secret_name(server, RELEASE)] = {
            "auth-config.json": json.dumps({"version": 1, "credentials": entries}, sort_keys=True)}
    idempotency, feast = token(), token()
    values["coordinator-idempotency-redis-auth-staging"] = {
        "password": idempotency, "connection_url": f"redis://default:{idempotency}@{IDEMPOTENCY_HOST}/0",
        "users.acl": IDEMPOTENCY_ACL.format(password=idempotency) + "\n"}
    values["feast-staging-redis-auth"] = {"password": feast}
    values["feast-online-redis-staging-fraud"] = {"connection_string": f"{FEAST_REDIS_HOST},password={feast},db=1"}
    values["feast-online-redis-staging-rag"] = {"connection_string": f"{FEAST_REDIS_HOST},password={feast},db=4"}
    return _documents(SECRETS, values)


def plan() -> list[dict]:
    return [{"namespace": s.namespace, "name": s.name, "keys": list(s.keys), "consumer": s.consumer,
             "purpose": s.purpose, "level_a": spec_set} for spec_set, group in (("required", SECRETS), ("llm-only", LLM_ONLY))
            for s in group]


def _kubectl(context: str, args: Sequence[str], payload: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(["kubectl", "--context", context, *args], input=payload, capture_output=True, text=True,
                            timeout=120)
    if result.returncode:
        raise RuntimeError((result.stderr.strip().splitlines() or ["kubectl failed"])[-1][:300])
    return result


def existing(context: str, specs: Sequence[SecretSpec] = SECRETS) -> list[str]:
    found = []
    for namespace in sorted({s.namespace for s in specs}):
        names = _kubectl(context, ["-n", namespace, "get", "secret", "-o", "name"]).stdout.split()
        wanted = {s.name for s in specs if s.namespace == namespace}
        found += [f"{namespace}/{name.split('/', 1)[1]}" for name in names if name.split("/", 1)[1] in wanted]
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    for name in ("create", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("--context", required=True)
        command.add_argument("--approve", action="store_true")
        command.add_argument("--profile", choices=sorted(PROFILES), default="level-a")
    args = parser.parse_args(argv)
    if args.command == "plan":
        print(json.dumps(plan(), indent=1))
        return 0
    try:
        present = existing(args.context, PROFILES[args.profile])
        if args.command == "create":
            if present:
                raise RuntimeError(f"refusing to overwrite or merge: already present {present}")
            documents = manifests() if args.profile == "level-a" else llm_manifests()
            print(json.dumps([f"{d['metadata']['namespace']}/{d['metadata']['name']}" for d in documents]))
            if args.approve:
                payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": documents})
                print(_kubectl(args.context, ["create", "-f", "-", "-o", "name"], payload=payload).stdout.strip())
            else:
                print("plan only: re-run with --approve to create these Secrets", file=sys.stderr)
        else:
            print(json.dumps(present))
            if args.approve:
                for item in present:
                    namespace, name = item.split("/", 1)
                    _kubectl(args.context, ["-n", namespace, "delete", "secret", name, "--ignore-not-found"])
                print(f"revoked {len(present)} staging Secrets")
    except RuntimeError as error:
        print(f"staging_credentials: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
