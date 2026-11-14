"""Publish embedded fraud-policy chunks through Feast: offline parquet → online store.

The DAG writes one immutable parquet per ``feature_index_revision`` as the
``fraud_policy_chunks`` FileSource, applies the entity/feature view to the
registry (partial apply: other feature views are untouched) and materializes
it into the online store. Readback is a ``get_online_features`` key lookup of
the canonical chunk ids; Feast/Redis similarity search is not used or claimed
(``feast_contract``).

Profiles:
- ``redis``: canonical. The Feast Online Redis
  (``EWALLET_REDIS_CONNECTION_STRING``, which must select its database) with a
  RAG-owned registry (``EWALLET_RAG_FEAST_REGISTRY``, project
  ``EWALLET_RAG_FEAST_PROJECT``). ``online_target`` decides which database and
  project a run may use and claims the database before anything is written. The MLOps-published online registry of the
  historical fraud features is never written; feature-rag-mcp opens the same
  RAG registry (``tools.service``) to read the rows the DAG published.
- ``sqlite``: explicit local-dev profile; registry and online DB live under
  the publication root. Never presented as the canonical online store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

ROW_KEYS = ("chunk_id", "document_id", "chunk_text", "source_uri", "source_checksum", "corpus_revision",
            "embedding_model_revision", "feature_index_revision")
ONLINE_STORES = ("redis", "sqlite")
LOCAL_PROJECT = "ewallet_rag_local"
DEFAULT_RAG_PROJECT = "ewallet_rag"


@dataclass(frozen=True)
class FeastTarget:
    online_store: str
    project: str
    registry: str
    publication_root: str
    redis_connection: str | None = None

    def __post_init__(self) -> None:
        if self.online_store not in ONLINE_STORES:
            raise ValueError(f"online_store must be one of {ONLINE_STORES}")
        for name in ("project", "registry", "publication_root"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.online_store == "redis" and not (self.redis_connection or "").strip():
            raise ValueError("the redis profile requires a Redis connection string")

    @property
    def source_path(self) -> str:
        return self.publication_root.rstrip("/") + "/chunks.parquet"

    def describe(self) -> dict[str, str]:
        """Evidence-safe description: never includes the Redis connection string."""
        return {"online_store": self.online_store, "project": self.project, "registry": self.registry,
                "offline_source": self.source_path,
                "profile": "canonical" if self.online_store == "redis" else "local-dev"}


def resolve_target(*, online_store: str, publication_base: str, feature_index_revision: str,
                   environment: Mapping[str, str]) -> FeastTarget:
    """Build the publication target; missing canonical configuration fails here."""
    publication_root = f"{publication_base.rstrip('/')}/{feature_index_revision}"
    if online_store == "sqlite":
        return FeastTarget("sqlite", LOCAL_PROJECT, f"{publication_root}/registry.db", publication_root)
    from ai_platform.llm_agent.rag import online_target

    missing = [name for name in ("EWALLET_RAG_FEAST_REGISTRY", "EWALLET_REDIS_CONNECTION_STRING")
               if not environment.get(name, "").strip()]
    if missing:
        raise ValueError(f"redis profile is missing configuration: {missing}")
    project = environment.get("EWALLET_RAG_FEAST_PROJECT", "").strip() or DEFAULT_RAG_PROJECT
    # Explicit database, mode-appropriate database and project; raises before any write.
    online_target.resolve(environment["EWALLET_REDIS_CONNECTION_STRING"], project=project, environment=environment)
    return FeastTarget("redis", project, environment["EWALLET_RAG_FEAST_REGISTRY"], publication_root,
                       environment["EWALLET_REDIS_CONNECTION_STRING"])


def repo_config(target: FeastTarget) -> Any:
    from feast.repo_config import RepoConfig

    if target.online_store == "redis":
        online: dict[str, Any] = {"type": "redis", "connection_string": target.redis_connection,
                                  "key_ttl_seconds": None}
    else:
        online = {"type": "sqlite", "path": f"{target.publication_root}/online.db"}
    provider = "gcp" if target.registry.startswith("gs://") else "local"
    return RepoConfig(project=target.project, provider=provider, registry=target.registry,
                      offline_store={"type": "file"}, online_store=online, entity_key_serialization_version=3)


def publication_identity(rows: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 over chunk identity, text, provenance and revisions (not floats)."""
    canonical = sorted(([str(row[key]) for key in ROW_KEYS] for row in rows), key=lambda item: item[0])
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def publication_frame(rows: Sequence[Mapping[str, Any]], *, dimensions: int) -> Any:
    import numpy as np
    import pandas as pd

    if not rows:
        raise ValueError("nothing to publish")
    ids = [str(row["chunk_id"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk_id in publication rows")
    frame = pd.DataFrame([{key: str(row[key]) for key in ROW_KEYS} for row in rows])
    vectors = [np.asarray(row["embedding"], dtype=np.float32) for row in rows]
    if any(vector.shape != (dimensions,) or not np.isfinite(vector).all() or not vector.any() for vector in vectors):
        raise ValueError(f"every embedding must be a finite non-zero {dimensions}-d vector")
    frame["embedding"] = vectors
    frame["event_timestamp"] = pd.to_datetime([row["event_timestamp"] for row in rows], utc=True)
    frame["publication_timestamp"] = pd.to_datetime([row["publication_timestamp"] for row in rows], utc=True)
    return frame.sort_values("chunk_id", ignore_index=True)


def _existing_frame(path: str) -> Any | None:
    import fsspec
    import pandas as pd

    filesystem, location = fsspec.core.url_to_fs(path)
    if not filesystem.exists(location):
        return None
    return pd.read_parquet(path)


def write_offline_source(frame: Any, target: FeastTarget) -> str:
    """Write the immutable parquet once; an identical re-run reuses it.

    Returns ``WRITTEN`` or ``REUSED``. A different payload under the same
    ``feature_index_revision`` fails: a new corpus needs a new revision.
    """
    import fsspec
    import numpy as np

    existing = _existing_frame(target.source_path)
    if existing is not None:
        existing = existing.sort_values("chunk_id", ignore_index=True)
        same_rows = list(existing.chunk_id) == list(frame.chunk_id) and all(
            (existing[key].astype(str) == frame[key]).all() for key in ROW_KEYS)
        same_vectors = same_rows and all(
            np.allclose(np.asarray(left, dtype=np.float32), right, atol=1e-5)
            for left, right in zip(existing.embedding, frame.embedding))
        if not same_vectors:
            raise ValueError(f"{target.source_path} already holds a different publication for this revision")
        return "REUSED"
    filesystem, location = fsspec.core.url_to_fs(target.publication_root)
    filesystem.makedirs(location, exist_ok=True)
    frame.to_parquet(target.source_path, index=False)
    return "WRITTEN"


def apply_and_materialize(store: Any, frame: Any, target: FeastTarget, *, now: datetime | None = None) -> dict[str, Any]:
    from ai_platform.features.feast.feature_repo.rag_feature_definitions import (
        RAG_FEATURE_VIEW,
        build_rag_feature_objects,
    )

    entity, view = build_rag_feature_objects(target.source_path)
    store.apply([entity, view])
    end = now or datetime.now(timezone.utc)
    start = min(frame["event_timestamp"]).to_pydatetime() - timedelta(seconds=1)
    if start >= end:
        raise ValueError("chunk event_timestamp is in the future; refusing to materialize")
    store.materialize(start_date=start, end_date=end, feature_views=[RAG_FEATURE_VIEW])
    return {"feature_view": RAG_FEATURE_VIEW, "materialize_start": start.isoformat(),
            "materialize_end": end.isoformat()}


def online_readback(store: Any, chunk_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Rows for ``chunk_ids`` from the online store, in the same order."""
    from ai_platform.llm_agent.rag.online_retriever import FeastChunkRowSource

    return FeastChunkRowSource(store, timeout_seconds=30.0)._fetch(list(chunk_ids))
