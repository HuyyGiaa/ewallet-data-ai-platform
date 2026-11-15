"""Publisher side of the Feast cross-version check (the feature repo's locked Feast, 0.66.0).

Applies the real online-repo and RAG definitions to a local file registry and writes one
deterministic row per entity into a throwaway Redis, as streaming-sinks and the RAG
ingestion DAG do in production. Writes the expected values to ``expected.json``.
Usage: writer.py <work-dir> <redis-host:port>
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import pandas as pd


def main(work: Path, redis: str) -> None:
    os.environ["EWALLET_FEAST_OFFLINE_DATA_ROOT"] = str(work / "offline")
    import feast
    from feast import FeatureStore
    from feast.repo_config import RepoConfig

    from ai_platform.features.feast.feature_repo.rag_feature_definitions import build_rag_feature_objects
    from ai_platform.features.feast.online_repo.feature_definitions import build_online_feature_objects
    from ai_platform.features.feast.schema import FEATURE_SPECS

    stamp = datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc)
    expected: dict[str, object] = {"writer_feast": feast.__version__, "fraud": {}, "rag": {}}

    fraud = FeatureStore(config=RepoConfig(
        project="ewallet_fraud_online", provider="local", registry=str(work / "fraud-registry.db"),
        online_store={"type": "redis", "connection_string": f"{redis},db=0"}, entity_key_serialization_version=3))
    entities, _, pushes, views = build_online_feature_objects(str(work / "offline"))
    fraud.apply([*entities.values(), *pushes.values(), *views.values()])
    for view_name, spec in FEATURE_SPECS.items():
        integers = set(spec["integer_features"])
        row = {spec["entity_key"]: f"{spec['entity_name']}-compat-1", "event_timestamp": stamp}
        for index, feature in enumerate(spec["features"]):
            row[feature] = (index + 7) if feature in integers else (index + 0.25)
        fraud.write_to_online_store(view_name, pd.DataFrame([row]))
        expected["fraud"][view_name] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}

    rag = FeatureStore(config=RepoConfig(
        project="ewallet_rag_compat", provider="local", registry=str(work / "rag-registry.db"),
        online_store={"type": "redis", "connection_string": f"{redis},db=1"}, entity_key_serialization_version=3))
    entity, view = build_rag_feature_objects(str(work / "offline" / "rag.parquet"))
    rag.apply([entity, view])
    chunk = {"chunk_id": "chunk-compat-1", "event_timestamp": stamp, "publication_timestamp": stamp,
             "document_id": "doc-1", "chunk_text": "Step-up authentication is required for account takeover risk.",
             "source_uri": "gs://compat/doc-1.md", "source_checksum": "sha256:" + "0" * 64, "corpus_revision": "c1",
             "embedding_model_revision": "e1", "feature_index_revision": "f1", "embedding": [0.5, -0.25, 0.125, 1.0]}
    fields = {field.name for field in view.schema}
    rag.write_to_online_store(view.name, pd.DataFrame([{k: v for k, v in chunk.items()
                                                        if k in fields | {"chunk_id", "event_timestamp", "publication_timestamp"}}]))
    expected["rag"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in chunk.items()}
    (work / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True))
    print(f"writer feast {feast.__version__}: applied {len(views)} fraud views and 1 RAG view, wrote {len(views) + 1} rows")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
