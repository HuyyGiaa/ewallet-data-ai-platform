"""Seed the Agent staging Feast Online Redis with exactly the rows the staging E2E reads (CI/CD V2-P3B).

The staging known answers (jenkins/cd/agent-staging.contract.json, copied from the sandbox candidate) were
produced by the production feature-rag-mcp reading:

* Fraud: Feast project ``ewallet_fraud_online`` in the Feature Store Redis DB 0, for the four entities of the
  fixture transaction (configs/experiments/fixtures/agent-load-request.json: user, account, device, merchant);
* RAG:   the verification project ``ewallet_rag_hybrid_20261114t144825`` in DB 3 (the whole published corpus,
  a handful of keys).

Re-materializing from an offline snapshot would not reproduce those exact online rows (window and run
timestamps differ), so this tool copies the rows without decoding them:

    source DUMP (read-only)  ->  target RESTORE (create-only, never REPLACE)  ->  digest(source) == digest(target)

The digest is over the logical content, not the DUMP payload: a hash in ``hashtable`` encoding (any value longer
than hash-max-listpack-value, e.g. the RAG embedding rows) is serialized in dict iteration order, which depends on
each server's hash seed, so the same hash DUMPs to different bytes on the source and on the target after RESTORE
(G.5.6 forensics). ``canonical`` reads TYPE, PTTL and the value (string bytes, or every hash field/value sorted by
field) and frames each part with its length, so binary data is compared exactly and order does not matter.

Bounds, all fail-closed before any write:

* fraud keys are computed with Feast's own Redis key function (project, entity key, serialization version 3) for
  the fixture entities only; a missing key is an error, never skipped;
* RAG keys are the whole verification database, at most ``MAX_RAG_KEYS``, and its Feature Store ownership marker
  must name the RAG project the staging MCP server reads;
* endpoint identity is checked on the parsed connection strings, before any network I/O: the source must name
  the Feature Store Service ``redis.feature-store.svc.cluster.local:6379`` in DB 0 (fraud) or DB 3 (RAG), the target
  the staging Service ``feast-staging-redis.ewallet-agents-staging.svc.cluster.local:6379`` in DB 1 (fraud) or DB 4
  (RAG). Any other host (``127.0.0.1`` included), port or database is refused;
* the target database must be empty, and source and target must be different Redis servers (``INFO`` run_id);
* connection strings come from the environment, are never printed or written.

Two ways to reach the Services:

* direct (in-cluster): the client connects to the Service DNS names it was given and checked;
* ``--context CTX`` (from a workstation): this process starts its own ``kubectl port-forward`` to exactly
  ``feature-store/svc/redis`` and ``ewallet-agents-staging/svc/feast-staging-redis`` in that context
  (scripts/ci/sandbox_candidate.kubectl_forward reads the local port from its own child), and connects only there.
  An operator-started port-forward is never accepted: nothing would bind its local port to a Service.

    python -m ops.llm_agent.staging_feature_seed plan
    EWALLET_SEED_SOURCE=<conn> EWALLET_SEED_TARGET=<conn> python -m ops.llm_agent.staging_feature_seed copy fraud --context CTX [--approve]
    EWALLET_SEED_SOURCE=<conn> EWALLET_SEED_TARGET=<conn> python -m ops.llm_agent.staging_feature_seed verify fraud --context CTX

``<conn>`` is a Feast Redis setting naming the Service DNS (``<service host>:6379,password=...,db=N``).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "configs/experiments/fixtures/agent-load-request.json"
FRAUD_PROJECT = "ewallet_fraud_online"
RAG_PROJECT = "ewallet_rag_hybrid_20261114t144825"
ENTITY_KEY_SERIALIZATION_VERSION = 3
# Join key per Feast entity (ai_platform/features/feast/schema.py FEATURE_SPECS).
JOIN_KEYS = ("user_id", "account_id", "device_id", "merchant_id")
SOURCE_HOST = "redis.feature-store.svc.cluster.local"
TARGET_HOST = "feast-staging-redis.ewallet-agents-staging.svc.cluster.local"
REDIS_PORT = 6379
# (namespace, Service) a --context port-forward is opened to; the only two Services this tool ever reaches.
SERVICES = {"source": ("feature-store", "redis"), "target": ("ewallet-agents-staging", "feast-staging-redis")}
SOURCE_DB = {"fraud": 0, "rag": 3}
TARGET_DB = {"fraud": 1, "rag": 4}
MAX_RAG_KEYS = 64
DIGEST_SCHEME = "canonical-v1"   # TYPE + PTTL -1 + sorted, length-framed value (not the DUMP payload)
OWNER_KEY = b"__ewallet_feature_store_campaign_owner_v1__"


class SeedError(RuntimeError):
    """The copy would leave its bounds; nothing was written."""


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


def check_endpoints(kind: str, source: Any, target: Any) -> None:
    """``source``/``target`` are the parsed connection strings (RedisEndpoint), never operator assertions."""

    if (source.host, source.port, source.database) != (SOURCE_HOST, REDIS_PORT, SOURCE_DB[kind]):
        raise SeedError(f"{kind} source must be {SOURCE_HOST}:{REDIS_PORT} DB {SOURCE_DB[kind]}")
    if (target.host, target.port, target.database) != (TARGET_HOST, REDIS_PORT, TARGET_DB[kind]):
        raise SeedError(f"{kind} target must be the staging Redis {TARGET_HOST}:{REDIS_PORT} DB {TARGET_DB[kind]}")


def check_distinct(source: Any, target: Any) -> None:
    """Two Service names could still route to one server; a write target equal to the source is refused."""

    if source.info("server")["run_id"] == target.info("server")["run_id"]:
        raise SeedError("source and target are the same Redis server; refusing to write")


def _frame(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def canonical(client: Any, key: bytes) -> bytes:
    """Order-independent, unambiguous encoding of one key's logical content: type, item count, items.

    Only persistent strings and hashes are accepted (the fixture rows): a missing key, an expiring key or any other
    type is an error, never compared loosely.
    """

    kind = client.type(key)
    kind = kind.decode() if isinstance(kind, bytes) else str(kind)
    if kind == "none":
        raise SeedError(f"key {_key_id(key)} is missing")
    ttl = client.pttl(key)
    if ttl != -1:
        raise SeedError(f"key {_key_id(key)} has PTTL {ttl}; only persistent keys (PTTL -1) are copied")
    if kind == "string":
        items = [client.get(key)]
    elif kind == "hash":
        items = [part for field, value in sorted(client.hgetall(key).items()) for part in (field, value)]
    else:
        raise SeedError(f"key {_key_id(key)} has unsupported type {kind}")
    if any(item is None for item in items):
        raise SeedError(f"key {_key_id(key)} changed while it was read")
    return _frame(kind.encode()) + len(items).to_bytes(8, "big") + b"".join(_frame(item) for item in items)


def digest(client: Any, keys: Iterable[bytes]) -> str:
    """SHA-256 over the sorted keys, each framed with its canonical content."""

    hasher = hashlib.sha256()
    for key in sorted(keys):
        hasher.update(_frame(key) + _frame(canonical(client, key)))
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
    source_digest = digest(source, keys)                # read-only on the source; fails on a missing/expiring key
    dumps = [(key, source.dump(key)) for key in keys]   # read-only on the source
    if any(payload is None for _, payload in dumps):
        raise SeedError("a source key disappeared before DUMP")
    if target.dbsize() != 0:
        raise SeedError(f"the staging {kind} database is not empty; refusing to mix data (clean it up first)")
    report = {"kind": kind, "keys": len(keys), "digest": DIGEST_SCHEME, "source_digest": source_digest,
              "written": False}
    if not approve:
        return report
    for key, payload in dumps:
        target.restore(key, 0, payload, replace=False)   # create-only: an existing key is an error
    target_digest = digest(target, keys)
    if target_digest != source_digest:
        raise SeedError("the staging copy differs from the source after RESTORE")
    return {**report, "written": True, "target_digest": target_digest}


def verify(kind: str, source: Any, target: Any) -> dict[str, Any]:
    keys = keys_for(kind, source)
    source_digest = digest(source, keys)
    target_digest = digest(target, keys)
    extra = target.dbsize() - len(keys)
    return {"kind": kind, "keys": len(keys), "digest": DIGEST_SCHEME, "source_digest": source_digest,
            "target_digest": target_digest,
            "extra_target_keys": extra, "verdict": "PASS" if source_digest == target_digest and extra == 0 else "FAIL"}


def _redis(host: str, port: int, endpoint: Any):
    import redis

    return redis.Redis(host=host, port=port, db=endpoint.database, password=endpoint.password, ssl=endpoint.ssl,
                       socket_timeout=10)


@contextlib.contextmanager
def _forward(context: str, namespace: str, service: str) -> Iterator[int]:
    """This process's own ``kubectl port-forward`` to one fixed Service; yields the local port."""

    from scripts.ci.sandbox_candidate import kubectl_forward

    with kubectl_forward(context, {"kagent": {"namespace": namespace, "service": service, "port": REDIS_PORT}}) as url:
        yield urlsplit(url).port


@contextlib.contextmanager
def connections(kind: str, source_connection: str, target_connection: str, context: str | None, *,
                redis_factory: Callable[..., Any] = _redis, forward: Callable[..., Any] = _forward) -> Iterator[tuple]:
    """Checked (source, target) clients. Every check runs before the first byte is sent to either server."""

    from ai_platform.llm_agent.rag.online_target import parse_connection

    endpoints = {"source": parse_connection(source_connection), "target": parse_connection(target_connection)}
    check_endpoints(kind, endpoints["source"], endpoints["target"])
    with contextlib.ExitStack() as stack:
        clients = []
        for role, endpoint in endpoints.items():
            if context:   # connect only through the port this process forwarded to the fixed Service
                port = stack.enter_context(forward(context, *SERVICES[role]))
                clients.append(redis_factory("127.0.0.1", port, endpoint))
            else:         # in-cluster: the Service DNS checked above
                clients.append(redis_factory(endpoint.host, endpoint.port, endpoint))
        check_distinct(*clients)
        yield tuple(clients)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="offline: the fixture entities and the exact fraud keys (no connection)")
    for name in ("copy", "verify"):
        command = commands.add_parser(name)
        command.add_argument("kind", choices=("fraud", "rag"))
        command.add_argument("--context", help="kube context: open this process's own port-forwards to the two "
                                               "fixed Services (omit only when running inside the cluster)")
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
        with connections(args.kind, os.environ["EWALLET_SEED_SOURCE"], os.environ["EWALLET_SEED_TARGET"],
                         args.context) as (source, target):
            report = copy(args.kind, source, target, approve=args.approve) if args.command == "copy" \
                else verify(args.kind, source, target)
    except (SeedError, KeyError, ValueError, RuntimeError) as error:
        print(f"staging_feature_seed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("verdict", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
