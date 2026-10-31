"""Feast publication and vector-retrieval compatibility gate."""

from __future__ import annotations

from dataclasses import dataclass


PINNED_FEAST_VERSION = "0.66.0"


@dataclass(frozen=True)
class FeastVectorCompatibility:
    feast_version: str
    online_store: str
    array_float32_supported: bool
    vector_search_supported: bool
    status: str
    blocker: str | None


def assess_vector_compatibility(*, feast_version: str, online_store: str) -> FeastVectorCompatibility:
    if feast_version != PINNED_FEAST_VERSION:
        raise ValueError(f"expected Feast {PINNED_FEAST_VERSION}, got {feast_version}")
    normalized = online_store.strip().lower()
    supported = normalized in {"milvus", "qdrant", "pgvector", "elasticsearch", "sqlite", "faiss"}
    blocker = None if supported else (
        f"Feast publication can carry Array(Float32), but {normalized or 'unspecified'} "
        "is not an approved vector-search backend for this release"
    )
    return FeastVectorCompatibility(
        feast_version=feast_version,
        online_store=normalized,
        array_float32_supported=True,
        vector_search_supported=supported,
        status="COMPATIBLE" if supported else "PUBLICATION_ONLY_RUNTIME_BLOCKED",
        blocker=blocker,
    )


def datahub_lineage_plan(*, corpus_revision: str, feature_index_revision: str) -> dict[str, object]:
    return {
        "operation": "TRANSFORMED",
        "upstream": f"urn:li:dataset:(urn:li:dataPlatform:file,fraud-policy/{corpus_revision},PROD)",
        "downstream": f"urn:li:dataset:(urn:li:dataPlatform:feast,fraud_policy_chunks/{feature_index_revision},PROD)",
        "required_readback": ["upstreamLineage", "corpus_revision", "feature_index_revision"],
        "runtime_verified": False,
    }
