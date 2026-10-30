"""Feast 0.66 definitions for versioned fraud-policy embedding publication.

Registration and vector retrieval remain runtime-gated. The configured Redis
online store is not declared equivalent to a vector-search backend.
"""

from __future__ import annotations

import os
from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource, ValueType
from feast.types import Array, Float32, String


rag_chunk = Entity(
    name="rag_chunk",
    join_keys=["chunk_id"],
    value_type=ValueType.STRING,
    description="Immutable fraud-policy chunk",
)

rag_source = FileSource(
    name="fraud_policy_chunks_source",
    path=os.getenv("EWALLET_RAG_FEAST_DATA_ROOT", "data/rag/fraud-policy-v1.parquet"),
    timestamp_field="event_timestamp",
    created_timestamp_column="publication_timestamp",
)

fraud_policy_chunks = FeatureView(
    name="fraud_policy_chunks",
    entities=[rag_chunk],
    ttl=timedelta(days=3650),
    schema=[
        Field(name="document_id", dtype=String),
        Field(name="chunk_text", dtype=String),
        Field(name="source_uri", dtype=String),
        Field(name="source_checksum", dtype=String),
        Field(name="corpus_revision", dtype=String),
        Field(name="embedding_model_revision", dtype=String),
        Field(name="feature_index_revision", dtype=String),
        Field(
            name="embedding",
            dtype=Array(Float32),
            vector_index=True,
            vector_search_metric="COSINE",
        ),
    ],
    source=rag_source,
    online=True,
    enable_validation=True,
    tags={
        "domain": "fraud-policy-rag",
        "feast_version": "0.66.0",
        "runtime_status": "registration-pending",
    },
)
