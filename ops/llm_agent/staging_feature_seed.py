"""Seed the Agent staging Feast Online Redis with exactly the rows the staging E2E reads (CI/CD V2-P3B).

The staging known answers (jenkins/cd/agent-staging.contract.json, copied from the sandbox candidate) were
produced by the production feature-rag-mcp reading:

* Fraud: Feast project ``ewallet_fraud_online`` in the Feature Store Redis DB 0, for the four entities of the
  fixture transaction (configs/experiments/fixtures/agent-load-request.json: user, account, device, merchant);
* RAG:   the verification project ``ewallet_rag_hybrid_20261114t144825`` in DB 3 (the whole published corpus,
  a handful of keys).

Re-materializing from an offline snapshot would not reproduce those exact online rows (window and run
timestamps differ), so this tool copies the rows byte for byte, without decoding them:

    source DUMP (read-only)  ->  target RESTORE (create-only, never REPLACE)  ->  digest(source) == digest(target)

Bounds, all fail-closed before any write:

* fraud keys are computed with Feast's own Redis key function (project, entity key, serialization version 3) for
  the fixture entities only; a missing key is an error, never skipped;
* RAG keys are the whole verification database, at most ``MAX_RAG_KEYS``, and its Feature Store ownership marker
  must name the RAG project the staging MCP server reads;
* the source must be the Feature Store Redis in DB 0 (fraud) or DB 3 (RAG); the target must be the staging Redis
  (``feast-staging-redis.ewallet-agents-staging``) in DB 1 (fraud) or DB 4 (RAG), and empty;
* connection strings come from the environment (port-forwards), are never printed or written.

    python -m ops.llm_agent.staging_feature_seed plan
    EWALLET_SEED_SOURCE=<conn> EWALLET_SEED_TARGET=<conn> python -m ops.llm_agent.staging_feature_seed copy fraud --approve
    EWALLET_SEED_SOURCE=<conn> EWALLET_SEED_TARGET=<conn> python -m ops.llm_agent.staging_feature_seed verify fraud

``<conn>`` is a Feast Redis setting (``host:port,password=...,db=N``); through a port-forward the host is
127.0.0.1, so the operator also passes ``--source-host`` / ``--target-host`` naming the forwarded Service.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "configs/experiments/fixtures/agent-load-request.json"
FRAUD_PROJECT = "ewallet_fraud_online"
RAG_PROJECT = "ewallet_rag_hybrid_20261114t144825"
ENTITY_KEY_SERIALIZATION_VERSION = 3
# Join key per Feast entity (ai_platform/features/feast/schema.py FEATURE_SPECS).
JOIN_KEYS = ("user_id", "account_id", "device_id", "merchant_id")
SOURCE_HOST = "redis.feature-store.svc.cluster.local"
TARGET_HOST = "feast-staging-redis.ewallet-agents-staging.svc.cluster.local"
SOURCE_DB = {"fraud": 0, "rag": 3}
TARGET_DB = {"fraud": 1, "rag": 4}
MAX_RAG_KEYS = 64
OWNER_KEY = b"__ewallet_feature_store_campaign_owner_v1__"


class SeedError(RuntimeError):
    """The copy would leave its bounds; nothing was written."""


@dataclass(frozen=True)
class Endpoint:
    service_host: str   # the in-cluster Service the connection reaches (port-forward target)
    database: int


def fixture_entities(path: Path = FIXTURE) -> dict[str, str]:
    """The four join-key values of the fixture transaction the Feature-RAG E2E sends."""

    message = json.loads(path.read_text(encoding="utf-8"))["params"]["message"]["parts"][0]["text"]
    transaction = json.loads(message.split("REQUEST_JSON=", 1)[1].replace("__TASK_ID__", "fixture"))["transaction"]
    entities = {key: transaction.get(key) for key in JOIN_KEYS}
    missing = [key for key, value in entities.items() if not value]
    if missing:
        raise SeedError(f"fixture transaction lacks {missing}")
    return entities


def fraud_keys(entities: dict[str, str]) -> list[bytes]:
    """Feast's Redis keys for the fixture entities (one hash per entity, holding every view of that entity)."""

    from feast.infra.online_stores.helpers import _redis_key
    from feast.protos.feast.types.EntityKey_pb2 import EntityKey
    from feast.protos.feast.types.Value_pb2 import Value

    keys = []
    for join_key in JOIN_KEYS:
        entity = EntityKey(join_keys=[join_key], entity_values=[Value(string_val=entities[join_key])])
        keys.append(_redis_key(FRAUD_PROJECT, entity, entity_key_serialization_version=ENTITY_KEY_SERIALIZATION_VERSION))
    return keys


def check_endpoints(kind: str, source: Endpoint, target: Endpoint) -> None:
    if source.service_host != SOURCE_HOST or source.database != SOURCE_DB[kind]:
        raise SeedError(f"{kind} source must be {SOURCE_HOST} DB {SOURCE_DB[kind]}")
    if target.service_host != TARGET_HOST or target.database != TARGET_DB[kind]:
        raise SeedError(f"{kind} target must be the staging Redis {TARGET_HOST} DB {TARGET_DB[kind]}")


def digest(pairs: Iterable[tuple[bytes, bytes | None]]) -> str:
    """SHA-256 over sorted (key, DUMP payload); a missing key counts as an error, not as empty."""

    hasher = hashlib.sha256()
    for key, payload in sorted(pairs):
        if payload is None:
            raise SeedError(f"key {hashlib.sha256(key).hexdigest()[:16]} is missing")
        hasher.update(len(key).to_bytes(4, "big") + key + len(payload).to_bytes(8, "big") + payload)
    return hasher.hexdigest()


def keys_for(kind: str, source: Any) -> list[bytes]:
    if kind == "fraud":
        return fraud_keys(fixture_entities())
    keys = []
    for key in source.scan_iter(count=100):
        keys.append(key)
        if len(keys) > MAX_RAG_KEYS:
            raise SeedError(f"the RAG verification database holds more than {MAX_RAG_KEYS} keys; not a bounded copy")
    owner = source.get(OWNER_KEY)
    if owner is None or RAG_PROJECT.encode() not in owner:
        raise SeedError(f"the source RAG database is not owned by project {RAG_PROJECT}")
    return sorted(keys)


def copy(kind: str, source: Any, target: Any, *, approve: bool) -> dict[str, Any]:
    keys = keys_for(kind, source)
    dumps = [(key, source.dump(key)) for key in keys]   # read-only on the source
    source_digest = digest(dumps)
    if target.dbsize() != 0:
        raise SeedError(f"the staging {kind} database is not empty; refusing to mix data (clean it up first)")
    report = {"kind": kind, "keys": len(keys), "source_digest": source_digest, "written": False}
    if not approve:
        return report
    for key, payload in dumps:
        target.restore(key, 0, payload, replace=False)   # create-only: an existing key is an error
    target_digest = digest((key, target.dump(key)) for key in keys)
    if target_digest != source_digest:
        raise SeedError("the staging copy differs from the source after RESTORE")
    return {**report, "written": True, "target_digest": target_digest}


def verify(kind: str, source: Any, target: Any) -> dict[str, Any]:
    keys = keys_for(kind, source)
    source_digest = digest((key, source.dump(key)) for key in keys)
    target_digest = digest((key, target.dump(key)) for key in keys)
    extra = target.dbsize() - len(keys)
    return {"kind": kind, "keys": len(keys), "source_digest": source_digest, "target_digest": target_digest,
            "extra_target_keys": extra, "verdict": "PASS" if source_digest == target_digest and extra == 0 else "FAIL"}


def _client(connection: str):
    import redis

    from ai_platform.llm_agent.rag.online_target import parse_connection

    endpoint = parse_connection(connection)
    return endpoint, redis.Redis(host=endpoint.host, port=endpoint.port, db=endpoint.database,
                                 password=endpoint.password, ssl=endpoint.ssl, socket_timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="offline: the fixture entities and the exact fraud keys (no connection)")
    for name in ("copy", "verify"):
        command = commands.add_parser(name)
        command.add_argument("kind", choices=("fraud", "rag"))
        command.add_argument("--source-host", default=SOURCE_HOST, help="Service behind the source port-forward")
        command.add_argument("--target-host", default=TARGET_HOST, help="Service behind the target port-forward")
        if name == "copy":
            command.add_argument("--approve", action="store_true", help="write; without it only the plan is computed")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            entities = fixture_entities()
            keys = fraud_keys(entities)
            print(json.dumps({"fraud_project": FRAUD_PROJECT, "entities": entities,
                              "fraud_key_sha256": [hashlib.sha256(k).hexdigest() for k in keys],
                              "rag_project": RAG_PROJECT, "source_db": SOURCE_DB, "target_db": TARGET_DB,
                              "target_host": TARGET_HOST}, indent=1, sort_keys=True))
            return 0
        source_endpoint, source = _client(os.environ["EWALLET_SEED_SOURCE"])
        target_endpoint, target = _client(os.environ["EWALLET_SEED_TARGET"])
        check_endpoints(args.kind, Endpoint(args.source_host, source_endpoint.database),
                        Endpoint(args.target_host, target_endpoint.database))
        report = copy(args.kind, source, target, approve=args.approve) if args.command == "copy" \
            else verify(args.kind, source, target)
    except (SeedError, KeyError, ValueError) as error:
        print(f"staging_feature_seed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("verdict", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
