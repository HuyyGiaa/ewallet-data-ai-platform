"""Credential-free identity and exclusive campaign ownership for Feast Redis."""

from __future__ import annotations

import hashlib
import json
from typing import Mapping


OWNER_SCHEMA = "ewallet-redis-campaign-owner-v1"
OWNER_KEY = "__ewallet_feature_store_campaign_owner_v1__"


class RedisIsolationError(RuntimeError):
    """Redis cannot prove exclusive ownership by the requested campaign."""


def target_fingerprint(host: str, port: int, database: int, server_run_id: str) -> str:
    material = f"{host}:{port}/{database}:{server_run_id}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def owner_record(
    campaign_id: str,
    dataset_version: str,
    registry_uri: str,
    online_store_id: str,
    fingerprint: str,
    source_run_id: str,
    snapshot_id: str,
) -> dict[str, str]:
    return {
        "schema_version": OWNER_SCHEMA,
        "campaign_id": campaign_id,
        "dataset_version": dataset_version,
        "registry_uri": registry_uri,
        "online_store_id": online_store_id,
        "target_fingerprint": fingerprint,
        "source_run_id": source_run_id,
        "snapshot_id": snapshot_id,
    }


def claim(client, expected: Mapping[str, str]) -> str:
    """Atomically claim an empty database, or verify an identical prior claim."""

    encoded = json.dumps(dict(expected), sort_keys=True, separators=(",", ":"))
    script = """
local current = redis.call('GET', KEYS[1])
if current then
  if current == ARGV[1] then return 'OWNED' end
  return 'CONFLICT'
end
if redis.call('DBSIZE') ~= 0 then return 'AMBIGUOUS_NONEMPTY' end
redis.call('SET', KEYS[1], ARGV[1])
return 'CLAIMED'
"""
    status = client.eval(script, 1, OWNER_KEY, encoded)
    if isinstance(status, bytes):
        status = status.decode("utf-8")
    if status not in {"CLAIMED", "OWNED"}:
        raise RedisIsolationError(f"verification Redis isolation failed: {status}")
    return str(status)


def read_owner(client) -> dict[str, object] | None:
    value = client.get(OWNER_KEY)
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        record = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise RedisIsolationError("Redis ownership marker is invalid") from error
    if not isinstance(record, dict) or record.get("schema_version") != OWNER_SCHEMA:
        raise RedisIsolationError("Redis ownership marker has an unknown schema")
    return record
