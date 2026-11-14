"""Explicit, isolated Redis target for the RAG Feast publication.

The Feast Online Redis is shared: DB 0 serves production (including the
``ewallet_rag`` project read by the feature-rag-mcp tool), and other databases
belong to Feature Store verification campaigns. A connection string without a
database silently selects DB 0, so the RAG ingestion refuses it.

Two modes, chosen with ``EWALLET_RAG_ONLINE_TARGET``:

- ``verification`` (default): a database in 2–15, a dedicated Feast project
  (not ``ewallet_rag``), and exclusive ownership. Before writing, the run claims
  the database with the Feature Store's ownership marker
  (:mod:`ai_platform.features.feast.redis_isolation`): an empty database is
  claimed, a database already claimed by the identical campaign is reused, and
  anything else stops the run. Nothing is ever flushed or deleted.
- ``production``: DB 0 and the ``ewallet_rag`` project, i.e. the rows the
  production tool reads. It must be chosen explicitly; there is no claim,
  because DB 0 is never empty.

Evidence produced here never contains the password.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from ai_platform.features.feast import redis_isolation

MODE_VAR = "EWALLET_RAG_ONLINE_TARGET"
MODES = ("verification", "production")
PRODUCTION_DATABASE = 0
PRODUCTION_PROJECT = "ewallet_rag"
VERIFICATION_DATABASES = range(2, 16)
ONLINE_STORE_ID = "rag-verification"


class RagTargetError(ValueError):
    """The configured Redis target is not explicit, isolated or owned."""


@dataclass(frozen=True)
class RedisEndpoint:
    host: str
    port: int
    database: int
    password: str | None = None
    ssl: bool = False

    def __repr__(self) -> str:  # never show the password
        return f"RedisEndpoint(host={self.host!r}, port={self.port}, database={self.database}, ssl={self.ssl})"


def parse_connection(connection_string: str) -> RedisEndpoint:
    """Parse a Feast Redis setting and require an explicit database.

    Accepts ``host:port[,password=...][,ssl=true],db=N`` and
    ``redis[s]://[:password@]host:port/N``.
    """
    value = (connection_string or "").strip()
    if not value:
        raise RagTargetError("the Redis connection string is empty")
    if value.startswith(("redis://", "rediss://")):
        parsed = urlsplit(value)
        path = parsed.path.strip("/")
        if not parsed.hostname or not parsed.port:
            raise RagTargetError("the Redis URL must name host and port")
        if not path.isdigit():
            raise RagTargetError("the Redis URL must select a database explicitly (redis://host:port/N)")
        password = unquote(parsed.password) if parsed.password else None
        return RedisEndpoint(parsed.hostname, parsed.port, int(path), password, parsed.scheme == "rediss")
    first, *segments = value.split(",")
    host, separator, port = first.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise RagTargetError("the Redis connection must start with host:port")
    options: dict[str, str] = {}
    for segment in segments:
        key, equals, option = segment.partition("=")
        if not equals:
            raise RagTargetError("every Redis connection option must be key=value")
        options[key.strip()] = option
    if "db" not in options or not options["db"].strip().isdigit():
        raise RagTargetError("the Redis connection must select a database explicitly (,db=N); "
                             "without it Redis uses production DB 0")
    return RedisEndpoint(host, int(port), int(options["db"]), options.get("password") or None,
                         options.get("ssl", "").lower() == "true")


@dataclass(frozen=True)
class RagOnlineTarget:
    mode: str
    endpoint: RedisEndpoint
    project: str

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "redis_database": self.endpoint.database, "project": self.project}


def resolve(connection_string: str, *, project: str, environment: Mapping[str, str]) -> RagOnlineTarget:
    """Check the target against its mode; nothing is contacted here."""
    mode = environment.get(MODE_VAR, "").strip() or "verification"
    if mode not in MODES:
        raise RagTargetError(f"{MODE_VAR} must be one of {MODES}")
    endpoint = parse_connection(connection_string)
    if mode == "production":
        if endpoint.database != PRODUCTION_DATABASE or project != PRODUCTION_PROJECT:
            raise RagTargetError(f"production publication is DB {PRODUCTION_DATABASE} with project "
                                 f"{PRODUCTION_PROJECT} only")
    else:
        if endpoint.database not in VERIFICATION_DATABASES:
            raise RagTargetError(f"verification needs a database in {VERIFICATION_DATABASES.start}-"
                                 f"{VERIFICATION_DATABASES.stop - 1}; DB 0 is production and DB 1 is "
                                 "the canonical verification store")
        if project == PRODUCTION_PROJECT or not project.startswith(PRODUCTION_PROJECT + "_"):
            raise RagTargetError(f"verification needs its own Feast project named {PRODUCTION_PROJECT}_<suffix>")
    return RagOnlineTarget(mode, endpoint, project)


def check_storage_paths(target: RagOnlineTarget, *, registry: str, publication_base: str) -> None:
    """Shared GCS paths of a verification run must be its own.

    The bucket also holds the production registry and Parquet of ``ewallet_rag``
    (``features/registries/rag/fraud-policy-feast-v1/registry.db`` and
    ``features/rag/``), so a verification registry or publication base on
    ``gs://`` must contain the project as a path segment. Local paths are private
    to their machine or Pod and are not restricted.
    """
    if target.mode != "verification":
        return
    for name, value in (("registry", registry), ("publication base", publication_base)):
        if value.startswith("gs://") and f"/{target.project}/" not in value.rstrip("/") + "/":
            raise RagTargetError(f"the verification {name} on GCS must contain the path segment "
                                 f"/{target.project}/ so it cannot overwrite production or another run")


def redis_client(endpoint: RedisEndpoint, *, timeout_seconds: float = 10.0) -> Any:
    import redis

    return redis.Redis(host=endpoint.host, port=endpoint.port, db=endpoint.database, password=endpoint.password,
                       ssl=endpoint.ssl, socket_timeout=timeout_seconds, socket_connect_timeout=timeout_seconds)


def owner_record(target: RagOnlineTarget, client: Any, *, registry: str, corpus_revision: str,
                 feature_index_revision: str, manifest_sha256: str) -> dict[str, str]:
    run_id = str(client.info("server")["run_id"])
    fingerprint = redis_isolation.target_fingerprint(target.endpoint.host, target.endpoint.port,
                                                     target.endpoint.database, run_id)
    return redis_isolation.owner_record(
        campaign_id=target.project, dataset_version=corpus_revision, registry_uri=registry,
        online_store_id=ONLINE_STORE_ID, fingerprint=fingerprint, source_run_id=feature_index_revision,
        snapshot_id=manifest_sha256)


def preview_ownership(target: RagOnlineTarget, client: Any, *, registry: str, corpus_revision: str,
                      feature_index_revision: str, manifest_sha256: str) -> dict[str, Any]:
    """Read-only forecast of what :func:`ensure_ownership` would return; writes nothing.

    ``CLAIMABLE_EMPTY`` and ``OWNED_MATCH`` would succeed. ``CONFLICT`` lists the
    marker fields that differ (a changed ``target_fingerprint`` means another
    port, database or a restarted Redis server). ``NONEMPTY_UNOWNED`` means keys
    of another workload without a marker.
    """
    if target.mode == "production":
        return {**target.describe(), "ownership": "NOT_CLAIMED_PRODUCTION"}
    expected = owner_record(target, client, registry=registry, corpus_revision=corpus_revision,
                            feature_index_revision=feature_index_revision, manifest_sha256=manifest_sha256)
    current = redis_isolation.read_owner(client)
    size = int(client.dbsize())
    if current is None:
        status = "CLAIMABLE_EMPTY" if size == 0 else "NONEMPTY_UNOWNED"
        return {**target.describe(), "ownership": status, "dbsize": size}
    if current == expected:
        return {**target.describe(), "ownership": "OWNED_MATCH", "dbsize": size}
    differing = sorted(key for key in set(expected) | set(current) if current.get(key) != expected.get(key))
    return {**target.describe(), "ownership": "CONFLICT", "dbsize": size,
            "owner_campaign": current.get("campaign_id"), "differing_fields": differing}


def ensure_ownership(target: RagOnlineTarget, client: Any, *, registry: str, corpus_revision: str,
                     feature_index_revision: str, manifest_sha256: str) -> dict[str, Any]:
    """Claim (or confirm) the verification database; production is reported, never claimed."""
    if target.mode == "production":
        return {**target.describe(), "claim_status": "NOT_CLAIMED_PRODUCTION"}
    expected = owner_record(target, client, registry=registry, corpus_revision=corpus_revision,
                            feature_index_revision=feature_index_revision, manifest_sha256=manifest_sha256)
    try:
        status = redis_isolation.claim(client, expected)
    except redis_isolation.RedisIsolationError as error:
        raise RagTargetError(f"Redis DB {target.endpoint.database} is not available to {target.project}: "
                             f"{error}") from error
    return {**target.describe(), "claim_status": status, "target_fingerprint": expected["target_fingerprint"]}


def verify_ownership(target: RagOnlineTarget, client: Any, *, registry: str, corpus_revision: str,
                     feature_index_revision: str, manifest_sha256: str) -> dict[str, Any]:
    """Read-only check that the database is still owned by this campaign."""
    if target.mode == "production":
        return {**target.describe(), "owner": "NOT_CLAIMED_PRODUCTION"}
    expected = owner_record(target, client, registry=registry, corpus_revision=corpus_revision,
                            feature_index_revision=feature_index_revision, manifest_sha256=manifest_sha256)
    if redis_isolation.read_owner(client) != expected:
        raise RagTargetError(f"Redis DB {target.endpoint.database} is no longer owned by {target.project}")
    return {**target.describe(), "owner": "MATCH", "target_fingerprint": expected["target_fingerprint"]}
