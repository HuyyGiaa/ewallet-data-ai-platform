"""Read-only views of one RAG run: Feast registry, Online Store readback, run summary.

    python -m ops.llm_agent.rag_evidence feast   --run-dir <evidence root>/rag_<label>
    python -m ops.llm_agent.rag_evidence online  --run-dir <evidence root>/rag_<label>
    python -m ops.llm_agent.rag_evidence summary --run-dir <evidence root>/rag_<label>

Each command prints a terminal view (for the operator's screenshots) and, with
``--json PATH``, writes the same facts as JSON (create-only). They read the
run's artifacts, the Feast registry, the offline Parquet and the Online Store
named by the run's ``publication.json``; nothing is written to Feast, Redis or
DataHub. Configuration comes from ``$EWALLET_RAG_ENV_FILE`` exactly as for the
DAG tasks; no credential is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import math
from pathlib import Path
from typing import Any

from ops.llm_agent import rag_ingest


def _artifact(run: Path, name: str) -> dict[str, Any]:
    path = run / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _store(run: Path) -> tuple[Any, Any, dict[str, Any]]:
    from feast import FeatureStore

    from ai_platform.llm_agent.rag.feast_publication import repo_config
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex

    publication = _artifact(run, "publication.json")
    if not publication:
        raise FileNotFoundError(f"{run}/publication.json is missing; the publish task has not completed")
    index = TrustedChunkIndex.from_manifest(run / "source/manifest.json")
    target = rag_ingest._target(argparse.Namespace(online_store=publication["feast"]["online_store"]),
                                index.manifest)
    if target.describe() != publication["feast"]:
        raise ValueError("the configured Feast target differs from the one this run published to")
    return FeatureStore(config=repo_config(target)), target, publication


def feast_view(run: Path) -> dict[str, Any]:
    import pandas as pd

    from ai_platform.llm_agent.rag.feast_publication import publication_identity

    store, target, publication = _store(run)
    views = [{
        "name": view.name, "entities": list(view.entities), "online": view.online,
        "ttl_days": view.ttl.days if view.ttl else None, "source": view.batch_source.path,
        "fields": [{"name": field.name, "dtype": str(field.dtype)} for field in view.schema],
    } for view in store.list_feature_views()]
    entities = [{"name": entity.name, "join_keys": list(entity.join_keys)} for entity in store.list_entities()]
    frame = pd.read_parquet(target.source_path)
    rows = frame.drop(columns=["embedding"]).to_dict("records")
    identity = publication_identity(rows)
    return {
        "project": target.project, "registry": target.registry, "offline_source": target.source_path,
        "entities": entities, "feature_views": views, "offline_rows": len(frame),
        "embedding_dimensions": sorted({len(vector) for vector in frame["embedding"]}),
        "publication_identity": identity,
        "publication_identity_matches_run": identity == publication["publication_identity"],
        "materialization": publication["materialization"],
    }


def online_view(run: Path) -> dict[str, Any]:
    from ai_platform.llm_agent.rag.feast_publication import online_readback

    store, target, publication = _store(run)
    chunk_ids = [row["chunk_id"] for row in _artifact(run, "chunks.json")["chunks"]]
    rows = online_readback(store, chunk_ids)
    view: dict[str, Any] = {
        "project": target.project, "online_store": target.online_store,
        "online_target": publication.get("online_target"), "requested": len(chunk_ids),
        "rows": [{"chunk_id": chunk_id, "document_id": row.get("document_id"), "source_uri": row.get("source_uri"),
                  "corpus_revision": row.get("corpus_revision"),
                  "embedding_dimensions": len(row.get("embedding") or []),
                  "embedding_l2_norm": round(math.sqrt(sum(v * v for v in row.get("embedding") or [])), 6),
                  "text_preview": (row.get("chunk_text") or "")[:60]}
                 for chunk_id, row in zip(chunk_ids, rows)],
    }
    view["returned"] = sum(1 for row in view["rows"] if row["document_id"])
    if target.online_store == "redis":
        from ai_platform.features.feast import redis_isolation
        from ai_platform.llm_agent.rag import online_target

        resolved = online_target.resolve(target.redis_connection, project=target.project,
                                         environment=os.environ)
        client = online_target.redis_client(resolved.endpoint)
        owner = redis_isolation.read_owner(client) or {}
        view["redis"] = {"database": resolved.endpoint.database, "dbsize": client.dbsize(),
                         "owner_campaign": owner.get("campaign_id"), "owner_store": owner.get("online_store_id")}
    return view


def summary_view(run: Path) -> dict[str, Any]:
    documents, chunks, embeddings = (_artifact(run, name) for name in
                                     ("documents.json", "chunks.json", "embeddings.json"))
    publication, validation, datahub = (_artifact(run, name) for name in
                                        ("publication.json", "validation.json", "datahub.json"))
    states = _artifact(run, "airflow/task_states.json")
    return {
        "run": run.name,
        "airflow": {"run_id": (run / "airflow/run_id.txt").read_text().strip()
                    if (run / "airflow/run_id.txt").is_file() else None,
                    "task_states": {row.get("task_id"): row.get("state") for row in states}
                    if isinstance(states, list) else {}},
        "documents": {"count": documents.get("document_count"),
                      "checksum_mismatches": documents.get("checksum_mismatches")},
        "chunks": {"count": chunks.get("chunk_count"), "deterministic": chunks.get("deterministic")},
        "embeddings": {"model": embeddings.get("model_id"), "revision": embeddings.get("model_revision"),
                       "dimensions": embeddings.get("dimensions"), "invalid": embeddings.get("invalid_vectors")},
        "publication": {"rows": publication.get("row_count"), "identity": publication.get("publication_identity"),
                        "feast": publication.get("feast"), "online_target": publication.get("online_target")},
        "validation": {key: validation.get(key) for key in ("readback_status", "online_row_count", "recall_at_k",
                                                            "mean_reciprocal_rank", "citation_errors")},
        "datahub": {"status": datahub.get("status"), "aspects_read_back": len(datahub.get("verified", [])),
                    "assertions": {c["assertion_id"]: c["passed"] for c in datahub.get("quality_checks", [])},
                    "index_visibility": (datahub.get("index_visibility") or {}).get("status")},
    }


def _print(title: str, payload: dict[str, Any]) -> None:
    print(f"=== RAG {title} ===")
    print(json.dumps(payload, indent=2, sort_keys=False, default=str))


VIEWS = {"feast": ("FEAST PUBLICATION", feast_view), "online": ("ONLINE STORE READBACK", online_view),
         "summary": ("END-TO-END RUN SUMMARY", summary_view)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("view", choices=sorted(VIEWS))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--json", type=Path, help="also write the view as JSON (must not exist)")
    args = parser.parse_args(argv)
    rag_ingest.load_env_file()
    title, function = VIEWS[args.view]
    payload = function(args.run_dir.expanduser().resolve())
    _print(title, payload)
    if args.json:
        rag_ingest.write_once(args.json, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
