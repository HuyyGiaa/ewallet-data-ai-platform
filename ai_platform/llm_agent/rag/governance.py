"""DataHub lineage and data-quality assertions for the RAG ingestion DAG.

Records are pure (urn, aspect, payload) tuples built from the stage artifacts,
so they are tested without DataHub. Publication puts each aspect and reads it
back; a missing aspect or a mismatch fails the task. ``RagDataHubAdapter``
extends the existing ``verified_cloud_publisher.DataHubAdapter`` with the two
assertion aspects; the DataHub SDK is imported only by the adapter.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from data_platform.metadata.datahub.lineage.verified_cloud_publisher import flow_urn, job_urn

DAG_ID = "rag_fraud_policy"
ENV = "PROD"
ASSERTION_TYPE = "E-Wallet RAG Data Contract"
FEAST_FIELDS = (
    ("chunk_id", "string"), ("document_id", "string"), ("chunk_text", "string"), ("source_uri", "string"),
    ("source_checksum", "string"), ("corpus_revision", "string"), ("embedding_model_revision", "string"),
    ("feature_index_revision", "string"), ("embedding", "array<float32>"),
    ("event_timestamp", "timestamp"), ("publication_timestamp", "timestamp"),
)
# DAG task → (reads corpus, writes Feast feature view, reads Feast feature view)
TASK_IO = {
    "discover_documents": (True, False, False),
    "parse_and_chunk": (True, False, False),
    "generate_embeddings": (True, False, False),
    "publish_feast_features": (False, True, False),
    "validate_online_readback": (False, False, True),
}


class Adapter(Protocol):
    def put(self, urn: str, aspect: str, payload: dict[str, Any]) -> None: ...
    def get(self, urn: str, aspect: str) -> dict[str, Any] | None: ...


def dataset_urn(platform: str, name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{ENV})"


def corpus_urn(corpus_location: str, corpus_revision: str) -> str:
    platform = "gcs" if corpus_location.startswith("gs://") else "file"
    return dataset_urn(platform, f"fraud-policy/{corpus_revision}")


def feast_urn(project: str, feature_index_revision: str) -> str:
    return dataset_urn("feast", f"{project}.fraud_policy_chunks/{feature_index_revision}")


def assertion_urn(assertion_id: str, feature_index_revision: str) -> str:
    digest = hashlib.sha256(f"{DAG_ID}:{feature_index_revision}:{assertion_id}".encode()).hexdigest()[:32]
    return f"urn:li:assertion:{digest}"


@dataclass(frozen=True)
class QualityCheck:
    assertion_id: str
    description: str
    passed: bool
    observed: Mapping[str, Any]


def quality_checks(*, documents: Mapping[str, Any], chunks: Mapping[str, Any], embeddings: Mapping[str, Any],
                   publication: Mapping[str, Any], validation: Mapping[str, Any]) -> tuple[QualityCheck, ...]:
    """Governance checks derived only from what earlier tasks measured."""
    chunk_ids = [row["chunk_id"] for row in chunks["chunks"]]
    return (
        QualityCheck("source_checksums_match_manifest",
                     "Every source document's SHA-256 equals the versioned manifest",
                     documents["checksum_mismatches"] == 0 and documents["document_count"] > 0,
                     {"document_count": documents["document_count"],
                      "checksum_mismatches": documents["checksum_mismatches"]}),
        QualityCheck("chunk_id_unique_and_deterministic",
                     "chunk_id is unique and rebuilt identically from the manifest chunking parameters",
                     len(chunk_ids) == len(set(chunk_ids)) and chunks["deterministic"],
                     {"chunk_count": len(chunk_ids), "unique": len(set(chunk_ids)),
                      "deterministic": chunks["deterministic"]}),
        QualityCheck("embedding_dimension_finite_nonzero",
                     "Every embedding has the manifest dimension, finite values and unit L2 norm",
                     embeddings["invalid_vectors"] == 0 and embeddings["vector_count"] == len(chunk_ids),
                     {"dimensions": embeddings["dimensions"], "vector_count": embeddings["vector_count"],
                      "invalid_vectors": embeddings["invalid_vectors"]}),
        QualityCheck("online_rows_match_offline_publication",
                     "Feast Online returns every published chunk with identical provenance and revisions",
                     validation["readback_status"] == "MATCH"
                     and validation["online_row_count"] == publication["row_count"],
                     {"offline_rows": publication["row_count"], "online_rows": validation["online_row_count"],
                      "readback_status": validation["readback_status"]}),
        QualityCheck("retrieval_returns_cited_source",
                     "Each evaluation query retrieves at least one chunk citing a manifest document",
                     validation["empty_retrieval_rate"] == 0.0 and validation["citation_errors"] == 0,
                     {"recall_at_k": validation["recall_at_k"],
                      "empty_retrieval_rate": validation["empty_retrieval_rate"],
                      "citation_errors": validation["citation_errors"]}),
    )


def lineage_records(*, documents: Mapping[str, Any], embeddings: Mapping[str, Any], publication: Mapping[str, Any],
                    run_id: str, git_sha: str) -> list[tuple[str, str, dict[str, Any]]]:
    revision = publication["feature_index_revision"]
    source = corpus_urn(documents["source_location"], documents["corpus_revision"])
    target = feast_urn(publication["feast"]["project"], revision)
    records: list[tuple[str, str, dict[str, Any]]] = [
        (source, "datasetProperties", {
            "name": f"fraud-policy corpus {documents['corpus_revision']}",
            "qualifiedName": documents["source_location"],
            "description": "Versioned fraud-policy documents ingested by the rag_fraud_policy DAG",
            "customProperties": {
                "corpus_revision": documents["corpus_revision"],
                "document_count": str(documents["document_count"]),
                "manifest_sha256": documents["manifest_sha256"],
            },
        }),
        (target, "datasetProperties", {
            "name": f"fraud_policy_chunks {revision}",
            "qualifiedName": publication["feast"]["offline_source"],
            "description": "Fraud-policy chunks and embeddings published through Feast (offline parquet + online)",
            "customProperties": {
                "corpus_revision": documents["corpus_revision"],
                "feature_index_revision": revision,
                "embedding_model_id": embeddings["model_id"],
                "embedding_model_revision": embeddings["model_revision"],
                "embedding_dimensions": str(embeddings["dimensions"]),
                "row_count": str(publication["row_count"]),
                "publication_identity": publication["publication_identity"],
                "online_store": publication["feast"]["online_store"],
                "airflow_run_id": run_id,
                "source_git_sha": git_sha,
            },
        }),
        (target, "schemaMetadata", {
            "schemaName": "fraud_policy_chunks", "platform": "urn:li:dataPlatform:feast", "version": 0,
            "hash": hashlib.sha256(json.dumps(FEAST_FIELDS).encode()).hexdigest(),
            "rawSchema": json.dumps(FEAST_FIELDS),
            "fields": [{"fieldPath": name, "nativeDataType": kind, "nullable": False, "isPartitioningKey": False}
                       for name, kind in FEAST_FIELDS],
        }),
        (target, "upstreamLineage", {"upstreams": [{"dataset": source, "type": "TRANSFORMED"}]}),
        (flow_urn(DAG_ID), "dataFlowInfo", {
            "name": DAG_ID, "env": ENV,
            "customProperties": {"orchestrator": "airflow", "airflow_run_id": run_id, "source_git_sha": git_sha},
        }),
    ]
    for task_id, (reads_corpus, writes_feast, reads_feast) in TASK_IO.items():
        records.append((job_urn(DAG_ID, task_id), "dataJobInfo", {
            "name": task_id, "type": "BATCH", "flowUrn": flow_urn(DAG_ID),
            "customProperties": {"airflow_run_id": run_id, "source_git_sha": git_sha},
        }))
        records.append((job_urn(DAG_ID, task_id), "dataJobInputOutput", {
            "inputDatasets": sorted([source] * reads_corpus + [target] * reads_feast),
            "outputDatasets": [target] * writes_feast,
        }))
    return records


def assertion_records(checks: Sequence[QualityCheck], *, feast_dataset: str, feature_index_revision: str,
                      run_id: str, timestamp_millis: int) -> list[tuple[str, str, dict[str, Any]]]:
    records: list[tuple[str, str, dict[str, Any]]] = []
    for check in checks:
        urn = assertion_urn(check.assertion_id, feature_index_revision)
        records.append((urn, "assertionInfo", {
            "description": check.description, "entity": feast_dataset, "type": ASSERTION_TYPE,
            "logic": check.assertion_id,
        }))
        records.append((urn, "assertionRunEvent", {
            "timestampMillis": timestamp_millis, "runId": run_id, "asserteeUrn": feast_dataset,
            "result": "SUCCESS" if check.passed else "FAILURE",
            "observed": json.dumps(dict(check.observed), sort_keys=True, default=str),
        }))
    return records


# Timeseries aspects are indexed asynchronously (Kafka → search index), so the
# latest value may lag the write; versioned aspects are read back immediately.
TIMESERIES_ASPECTS = frozenset({"assertionRunEvent"})


def publish_with_readback(records: Sequence[tuple[str, str, dict[str, Any]]], adapter: Adapter, *,
                          timeseries_attempts: int = 30, delay_seconds: float = 1.0) -> list[dict[str, str]]:
    verified = []
    for urn, aspect, payload in records:
        adapter.put(urn, aspect, payload)
        attempts = timeseries_attempts if aspect in TIMESERIES_ASPECTS else 1
        for attempt in range(1, attempts + 1):
            observed = adapter.get(urn, aspect)
            if observed == payload:
                break
            if attempt < attempts:
                time.sleep(delay_seconds)
        else:
            raise RuntimeError(f"DataHub readback mismatch urn={urn} aspect={aspect}")
        verified.append({"urn": urn, "aspect": aspect, "readback": "MATCH", "attempts": str(attempt)})
    return verified


class RagDataHubAdapter:
    """The existing lineage adapter plus custom assertion aspects."""

    def __init__(self, url: str, token: str | None = None) -> None:
        from data_platform.metadata.datahub.lineage.verified_cloud_publisher import DataHubAdapter

        self._lineage = DataHubAdapter(url, token)

    def put(self, urn: str, aspect: str, payload: dict[str, Any]) -> None:
        if aspect not in ("assertionInfo", "assertionRunEvent"):
            self._lineage.put(urn, aspect, payload)
            return
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            AssertionInfoClass, AssertionResultClass, AssertionRunEventClass, AssertionRunStatusClass,
            AssertionTypeClass, CustomAssertionInfoClass,
        )

        if aspect == "assertionInfo":
            value: Any = AssertionInfoClass(
                type=AssertionTypeClass.CUSTOM, description=payload["description"],
                customAssertion=CustomAssertionInfoClass(
                    type=payload["type"], entity=payload["entity"], logic=payload["logic"]),
            )
        else:
            value = AssertionRunEventClass(
                timestampMillis=payload["timestampMillis"], runId=payload["runId"], assertionUrn=urn,
                asserteeUrn=payload["asserteeUrn"], status=AssertionRunStatusClass.COMPLETE,
                result=AssertionResultClass(type=payload["result"], nativeResults={"observed": payload["observed"]}),
            )
        self._lineage.emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=value))

    def get(self, urn: str, aspect: str) -> dict[str, Any] | None:
        if aspect not in ("assertionInfo", "assertionRunEvent"):
            return self._lineage.get(urn, aspect)
        from datahub.metadata.schema_classes import AssertionInfoClass, AssertionRunEventClass

        graph = self._lineage.graph
        if aspect == "assertionInfo":
            info = graph.get_aspect(entity_urn=urn, aspect_type=AssertionInfoClass)
            if info is None or info.customAssertion is None:
                return None
            return {"description": info.description, "entity": info.customAssertion.entity,
                    "type": info.customAssertion.type, "logic": info.customAssertion.logic}
        event = graph.get_latest_timeseries_value(entity_urn=urn, aspect_type=AssertionRunEventClass,
                                                  filter_criteria_map={})
        if event is None or event.result is None:
            return None
        return {"timestampMillis": event.timestampMillis, "runId": event.runId, "asserteeUrn": event.asserteeUrn,
                "result": str(event.result.type), "observed": (event.result.nativeResults or {}).get("observed")}
