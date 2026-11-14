"""Stage runner for the ``rag_fraud_policy_ingestion`` Airflow DAG.

Each DAG task runs one subcommand in the uv-locked RAG runtime
(``infra/docker/rag-ingestion``) and exchanges JSON artifacts through one run
directory ``<evidence root>/rag_<run_id>/`` (``$EWALLET_RAG_EVIDENCE_ROOT``, default
``docs/evidence/llm_agent``):

  discover  → documents.json   stage the versioned corpus, verify checksums
  chunk     → chunks.json      parse/clean, deterministic chunk ids
  embed     → embeddings.json  pinned ONNX sentence embeddings
  publish   → publication.json Feast offline parquet, apply, materialize
  validate  → validation.json  Feast Online readback + top-k retrieval/citations
  datahub   → datahub.json     lineage + quality assertions with readback

Artifacts are written atomically and only once, so a failed attempt leaves
nothing behind and a retry starts clean, while a completed stage is never
silently overwritten. Credentials come from the environment only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_PREFIX = "docs/evidence/llm_agent/rag_"
EVIDENCE_ROOT_VAR = "EWALLET_RAG_EVIDENCE_ROOT"
LOCKFILE = REPOSITORY_ROOT / "infra/docker/rag-ingestion/uv.lock"
RUNTIME_PACKAGES = (
    "feast", "gcsfs", "onnxruntime", "tokenizers", "huggingface-hub",
    "acryl-datahub", "pandas", "pyarrow",
)
DEFAULT_PUBLICATION_BASE = "data/rag/feast"


ENV_FILE_VAR = "EWALLET_RAG_ENV_FILE"
DEFAULT_ENV_FILE = Path.home() / ".config/ewallet-rag/runtime.env"
ENV_FILE_KEYS = frozenset({
    "EWALLET_RAG_FEAST_REGISTRY", "EWALLET_RAG_FEAST_PROJECT", "EWALLET_REDIS_CONNECTION_STRING",
    "EWALLET_RAG_PUBLICATION_BASE", "EWALLET_RAG_MODEL_CACHE", "EWALLET_RAG_EMBED_THREADS",
    "DATAHUB_GMS_URL", "DATAHUB_GMS_TOKEN", "HF_HOME", "GOOGLE_APPLICATION_CREDENTIALS",
    "EWALLET_RAG_ONLINE_TARGET",
})


def load_env_file(environment: dict[str, str] = os.environ) -> list[str]:  # type: ignore[assignment]
    """Fill unset allowlisted keys from ``$EWALLET_RAG_ENV_FILE`` (KEY=VALUE lines).

    Lets an operator configure Redis/Feast/DataHub for the DAG without
    restarting the shared Airflow scheduler. The file must exist and be
    private (no group/other permissions); values are never logged.
    """
    location = environment.get(ENV_FILE_VAR, "").strip()
    if not location:
        return []
    path = Path(location).expanduser()
    if not path.exists() and path == DEFAULT_ENV_FILE:
        return []
    if not path.is_file():
        raise FileNotFoundError(f"{ENV_FILE_VAR} points to a missing file: {path}")
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must not be readable by group or others (chmod 600)")
    loaded = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in ENV_FILE_KEYS:
            raise ValueError(f"{path}:{number}: only KEY=VALUE lines for {sorted(ENV_FILE_KEYS)} are allowed")
        if value.strip() and not environment.get(key):
            environment[key] = value.strip()
            loaded.append(key)
    return sorted(loaded)


def log(stage: str, message: str, **fields: Any) -> None:
    """One greppable line per event in the Airflow task log."""
    extra = " ".join(f"{key}={json.dumps(value, default=str)}" for key, value in fields.items())
    print(f"[rag:{stage}] {message} {extra}".rstrip(), flush=True)


def derive_run_id(*, run_label: str = "", airflow_run_id: str = "") -> str:
    """The explicit label, else the Airflow run id reduced to ``[a-z0-9-]``."""
    if run_label:
        return run_label
    cleaned = "".join(c if c.isascii() and c.isalnum() else "-" for c in airflow_run_id.lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)[:80].strip("-")
    if len(cleaned) < 3:
        raise ValueError("a run label or an Airflow run id is required")
    return cleaned


def evidence_root(environment: Mapping[str, str] = os.environ) -> Path:
    """Where run directories live: ``$EWALLET_RAG_EVIDENCE_ROOT``, else the repository's evidence folder.

    Local and canonical runs set it outside the checkout (the launchers default
    to ``~/ewallet-evidence/rag``), so repeated runs never dirty the repository;
    selected evidence is copied into ``docs/`` deliberately.
    """
    configured = environment.get(EVIDENCE_ROOT_VAR, "").strip()
    return Path(configured).expanduser() if configured else REPOSITORY_ROOT / Path(EVIDENCE_PREFIX).parent


def run_dir(run_id: str, *, root: Path | None = None) -> Path:
    """``<evidence root>/rag_<run_id>``; ``root`` is a repository root (tests)."""
    if not run_id or len(run_id) > 80 or not all(c.isalnum() or c in "_-" for c in run_id) or run_id[0] in "_-":
        raise ValueError(f"invalid run id: {run_id!r}")
    if root is not None:
        return root / f"{EVIDENCE_PREFIX}{run_id}"
    return evidence_root() / f"{Path(EVIDENCE_PREFIX).name}{run_id}"


def write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"{path.name} already exists for this run; use a new run id")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.link(partial, path)  # fails if another attempt won the race
    partial.unlink()


def read(directory: Path, name: str) -> dict[str, Any]:
    path = directory / name
    if not path.is_file():
        raise FileNotFoundError(f"upstream artifact {name} is missing in {directory}")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    # A CD image bakes its immutable source SHA and ships neither git nor .git.
    release = os.environ.get("EWALLET_RELEASE_SHA", "")
    if len(release) == 40 and all(c in "0123456789abcdef" for c in release):
        return release
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, capture_output=True,
                                text=True, check=False)
    except FileNotFoundError:
        return "unavailable"
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def runtime_identity() -> dict[str, Any]:
    versions = {}
    for name in RUNTIME_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": platform.python_version(), "executable": sys.executable, "packages": versions,
            "uv_lock_sha256": _sha256(LOCKFILE) if LOCKFILE.is_file() else None, "git_sha": _git_sha()}


def stage_corpus(source_location: str, destination: Path) -> Path:
    """Copy manifest + listed documents to the run directory; return the staged manifest."""
    import fsspec

    destination.mkdir(parents=True, exist_ok=True)
    base = source_location.rstrip("/")
    if not base.startswith("gs://"):
        base = str((REPOSITORY_ROOT / base).resolve())
    with fsspec.open(f"{base}/manifest.json", "rb") as stream:
        manifest_bytes = stream.read()
    (destination / "manifest.json").write_bytes(manifest_bytes)
    for entry in json.loads(manifest_bytes)["documents"]:
        relative = Path(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe document path: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with fsspec.open(f"{base}/{relative.as_posix()}", "rb") as stream:
            target.write_bytes(stream.read())
    return destination / "manifest.json"


def discover(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    from ai_platform.llm_agent.rag.pipeline import ingest_documents

    staged = stage_corpus(args.source_location, directory / "source")
    manifest, documents = ingest_documents(staged)  # raises on missing file or checksum mismatch
    log("discover", "verified corpus", corpus_revision=manifest.corpus_revision, documents=len(documents))
    return {
        "stage": "discover", "source_location": args.source_location,
        "manifest_sha256": _sha256(staged), "corpus_id": manifest.corpus_id,
        "corpus_revision": manifest.corpus_revision, "document_count": len(documents), "checksum_mismatches": 0,
        "documents": [{"document_id": doc.document_id, "source_uri": doc.source_uri, "sha256": doc.source_checksum,
                       "event_timestamp": doc.event_timestamp.isoformat(), "bytes": len(doc.text.encode()),
                       "words": len(doc.text.split())} for doc in documents],
        "runtime": runtime_identity(),
    }


def _chunks(directory: Path) -> tuple[Any, tuple[Any, ...]]:
    from ai_platform.llm_agent.rag.pipeline import chunk_documents, ingest_documents

    manifest, documents = ingest_documents(directory / "source/manifest.json")
    return manifest, chunk_documents(documents, size_words=manifest.chunk_size_words,
                                     overlap_words=manifest.chunk_overlap_words)


def chunk(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    read(directory, "documents.json")
    manifest, chunks = _chunks(directory)
    _, again = _chunks(directory)
    deterministic = [(c.chunk_id, c.text) for c in chunks] == [(c.chunk_id, c.text) for c in again]
    if not chunks or not deterministic:
        raise ValueError("chunking produced no chunks or is not deterministic")
    for document_id in sorted({c.document_id for c in chunks}):
        log("chunk", "document chunked", document_id=document_id,
            chunks=sum(c.document_id == document_id for c in chunks))
    return {
        "stage": "chunk", "corpus_revision": manifest.corpus_revision, "deterministic": deterministic,
        "release_sha": _git_sha(),
        "chunk_size_words": manifest.chunk_size_words, "chunk_overlap_words": manifest.chunk_overlap_words,
        "chunk_count": len(chunks),
        "chunks": [{"chunk_id": c.chunk_id, "document_id": c.document_id, "ordinal": c.ordinal,
                    "source_uri": c.source_uri, "source_checksum": c.source_checksum,
                    "corpus_revision": c.corpus_revision, "event_timestamp": c.event_timestamp.isoformat(),
                    "words": len(c.text.split()), "text": c.text} for c in chunks],
    }


def _embedder(manifest: Any) -> Any:
    from ai_platform.llm_agent.rag.embedding import OnnxSentenceEmbedder

    cache = os.environ.get("EWALLET_RAG_MODEL_CACHE")
    return OnnxSentenceEmbedder(manifest.embedding_model_id, manifest.embedding_model_revision,
                                manifest.embedding_dimensions, cache_dir=Path(cache) if cache else None,
                                threads=int(os.environ.get("EWALLET_RAG_EMBED_THREADS", "2")))


def embed(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    chunked = read(directory, "chunks.json")
    manifest, chunks = _chunks(directory)
    if [c.chunk_id for c in chunks] != [row["chunk_id"] for row in chunked["chunks"]]:
        raise ValueError("chunks.json differs from the staged corpus")
    embedder = _embedder(manifest)
    started = time.monotonic()
    vectors = embedder([c.text for c in chunks])
    norms = [math.sqrt(sum(v * v for v in vector)) for vector in vectors]
    invalid = sum(len(vector) != manifest.embedding_dimensions or not all(map(math.isfinite, vector))
                  or abs(norm - 1.0) > 1e-3 for vector, norm in zip(vectors, norms))
    if invalid:
        raise ValueError(f"{invalid} embeddings are invalid")
    log("embed", "embedded chunks", model=manifest.embedding_model_id, revision=manifest.embedding_model_revision,
        dimensions=manifest.embedding_dimensions, vectors=len(vectors), seconds=round(time.monotonic() - started, 3))
    return {
        "stage": "embed", "model_id": manifest.embedding_model_id, "model_revision": manifest.embedding_model_revision,
        "release_sha": _git_sha(),
        "model_file_sha256": embedder.model_sha256, "dimensions": manifest.embedding_dimensions,
        "normalization": "l2", "vector_count": len(vectors), "invalid_vectors": invalid,
        "min_norm": min(norms), "max_norm": max(norms),
        "vectors": {c.chunk_id: vector for c, vector in zip(chunks, vectors)},
    }


def _embedded_chunks(directory: Path) -> tuple[Any, tuple[Any, ...], dict[str, Any]]:
    from ai_platform.llm_agent.rag.pipeline import embed_chunks

    embedded_artifact = read(directory, "embeddings.json")
    manifest, chunks = _chunks(directory)
    vectors = embedded_artifact["vectors"]
    if set(vectors) != {c.chunk_id for c in chunks}:
        raise ValueError("embeddings.json does not cover the canonical chunk set")
    published_at = datetime.fromisoformat(embedded_artifact["completed_at"])
    embedded = embed_chunks(chunks, embed=lambda _: [vectors[c.chunk_id] for c in chunks],
                            dimensions=manifest.embedding_dimensions,
                            model_revision=manifest.embedding_model_revision,
                            index_revision=manifest.feature_index_revision, published_at=published_at)
    return manifest, embedded, embedded_artifact


def _target(args: argparse.Namespace, manifest: Any) -> Any:
    from ai_platform.llm_agent.rag.feast_publication import resolve_target

    base = os.environ.get("EWALLET_RAG_PUBLICATION_BASE", DEFAULT_PUBLICATION_BASE)
    if not base.startswith("gs://"):
        base = str((REPOSITORY_ROOT / base).resolve())
    return resolve_target(online_store=args.online_store, publication_base=base,
                          feature_index_revision=manifest.feature_index_revision, environment=os.environ)


def _online_ownership(target: Any, manifest: Any, directory: Path, *, claim: bool) -> dict[str, Any]:
    """Claim (publish) or re-check (validate) the Redis database; sqlite has nothing to own."""
    if target.online_store != "redis":
        return {"mode": "local-dev", "claim_status": "NOT_APPLICABLE_SQLITE"}
    from ai_platform.llm_agent.rag import online_target

    resolved = online_target.resolve(target.redis_connection, project=target.project, environment=os.environ)
    client = online_target.redis_client(resolved.endpoint)
    identity = {"registry": target.registry, "corpus_revision": manifest.corpus_revision,
                "feature_index_revision": manifest.feature_index_revision,
                "manifest_sha256": read(directory, "documents.json")["manifest_sha256"]}
    check = online_target.ensure_ownership if claim else online_target.verify_ownership
    result = check(resolved, client, **identity)
    log("publish" if claim else "validate", "online target", **result)
    return result


def publish(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    from feast import FeatureStore

    from ai_platform.llm_agent.rag.feast_publication import (
        apply_and_materialize, publication_frame, publication_identity, repo_config, write_offline_source,
    )
    from ai_platform.llm_agent.rag.pipeline import feast_publication_rows

    manifest, embedded, _ = _embedded_chunks(directory)
    rows = feast_publication_rows(embedded)
    target = _target(args, manifest)
    ownership = _online_ownership(target, manifest, directory, claim=True)
    frame = publication_frame(rows, dimensions=manifest.embedding_dimensions)
    offline_status = write_offline_source(frame, target)
    log("publish", "offline source", status=offline_status, path=target.source_path, rows=len(frame))
    window = apply_and_materialize(FeatureStore(config=repo_config(target)), frame, target)
    log("publish", "applied and materialized", **target.describe(), **window)
    return {"stage": "publish", "feature_index_revision": manifest.feature_index_revision,
            "corpus_revision": manifest.corpus_revision, "row_count": len(rows),
            "publication_identity": publication_identity(rows), "offline_status": offline_status,
            "release_sha": _git_sha(), "manifest_sha256": read(directory, "documents.json")["manifest_sha256"],
            "feast": target.describe(), "online_target": ownership, "materialization": window,
            "feast_vector_search": "NOT_USED (online store is read by chunk_id; similarity is computed in process)"}


def validate(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    import asyncio

    from feast import FeatureStore

    from ai_platform.llm_agent.rag.embedding import LocalQueryEmbedder
    from ai_platform.llm_agent.rag.feast_publication import repo_config
    from ai_platform.llm_agent.rag.online_retriever import FeastChunkRowSource, FeastOnlineExactRetriever
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex, to_agent_citations

    publication = read(directory, "publication.json")
    manifest, _, _ = _embedded_chunks(directory)
    target = _target(args, manifest)
    if target.describe() != publication["feast"]:
        raise ValueError("validation target differs from the published Feast target")
    ownership = _online_ownership(target, manifest, directory, claim=False)
    store = FeatureStore(config=repo_config(target))
    index = TrustedChunkIndex.from_manifest(directory / "source/manifest.json")
    embedder = _embedder(manifest)
    retriever = FeastOnlineExactRetriever(index, FeastChunkRowSource(store, timeout_seconds=30.0),
                                          LocalQueryEmbedder(embedder), cache_seconds=0.0)
    evaluation = json.loads((REPOSITORY_ROOT / args.evaluation).read_text(encoding="utf-8"))
    top_k, threshold = int(evaluation["top_k"]), float(evaluation["score_threshold"])

    async def run() -> list[dict[str, Any]]:
        await retriever.ready()  # every canonical chunk read back from the online store and verified
        results = []
        for query in evaluation["queries"]:
            hits = await retriever.search(str(query["text"]), top_k=top_k, min_score=threshold)
            try:
                to_agent_citations(hits, index=index)
                citation_error = None
            except ValueError as exc:
                citation_error = str(exc)
            expected = set(query["expected_document_ids"])
            results.append({
                "query_id": query["query_id"], "query": query["text"], "expected_document_ids": sorted(expected),
                "hit_expected": any(hit.document_id in expected for hit in hits),
                "reciprocal_rank": next((1.0 / rank for rank, hit in enumerate(hits, 1)
                                         if hit.document_id in expected), 0.0),
                "citation_error": citation_error,
                "citations": [{"rank": rank, **hit.__dict__} for rank, hit in enumerate(hits, 1)],
            })
        return results

    results = asyncio.run(run())
    count = len(results)
    report = {
        "stage": "validate", "feature_index_revision": manifest.feature_index_revision,
        "release_sha": publication.get("release_sha", _git_sha()),
        "readback_status": "MATCH", "online_row_count": len(index.chunks), "online_target": ownership,
        "retrieval_backend": retriever.backend_id, "top_k": top_k,
        "recall_at_k": sum(r["hit_expected"] for r in results) / count,
        "mean_reciprocal_rank": sum(r["reciprocal_rank"] for r in results) / count,
        "empty_retrieval_rate": sum(not r["citations"] for r in results) / count,
        "citation_errors": sum(r["citation_error"] is not None for r in results), "results": results,
    }
    log("validate", "online readback and retrieval", rows=report["online_row_count"], recall=report["recall_at_k"],
        mrr=report["mean_reciprocal_rank"], citation_errors=report["citation_errors"])
    for result in results:
        top = result["citations"][0] if result["citations"] else {}
        log("validate", "top hit", query_id=result["query_id"], chunk_id=top.get("chunk_id"),
            document_id=top.get("document_id"), source_uri=top.get("source_uri"), score=top.get("score"))
    if report["citation_errors"] or report["empty_retrieval_rate"] or report["recall_at_k"] < args.min_recall:
        raise ValueError(f"retrieval validation failed: recall_at_k={report['recall_at_k']} "
                         f"empty={report['empty_retrieval_rate']} citation_errors={report['citation_errors']}")
    return report


def datahub(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    from ai_platform.llm_agent.rag import governance

    documents, chunks = read(directory, "documents.json"), read(directory, "chunks.json")
    embeddings, publication = read(directory, "embeddings.json"), read(directory, "publication.json")
    validation = read(directory, "validation.json")
    checks = governance.quality_checks(documents=documents, chunks=chunks, embeddings=embeddings,
                                       publication=publication, validation=validation)
    feast_dataset = governance.feast_urn(publication["feast"]["project"], publication["feature_index_revision"])
    records = governance.lineage_records(documents=documents, embeddings=embeddings, publication=publication,
                                         run_id=args.run_id, git_sha=documents["runtime"]["git_sha"])
    records += governance.assertion_records(checks, feast_dataset=feast_dataset,
                                            feature_index_revision=publication["feature_index_revision"],
                                            run_id=args.run_id, timestamp_millis=int(time.time() * 1000))
    summary = {"stage": "datahub", "feast_dataset": feast_dataset, "record_count": len(records),
               "release_sha": publication.get("release_sha", _git_sha()),
               "quality_checks": [{"assertion_id": c.assertion_id, "passed": c.passed, "observed": dict(c.observed)}
                                  for c in checks]}
    if args.datahub_mode == "plan_only":
        # Explicit operator choice, recorded as NOT published; never reported as rubric evidence.
        log("datahub", "PLAN_ONLY: nothing published to DataHub", records=len(records))
        return {**summary, "status": "PLAN_ONLY_NOT_PUBLISHED",
                "records": [{"urn": u, "aspect": a, "payload": p} for u, a, p in records]}
    url = os.environ.get("DATAHUB_GMS_URL", "").strip()
    if not url:
        raise ValueError("DATAHUB_GMS_URL is not configured; set it or trigger with datahub_mode=plan_only")
    adapter = governance.RagDataHubAdapter(url, os.environ.get("DATAHUB_GMS_TOKEN") or None)
    verified = governance.publish_with_readback(records, adapter)
    log("datahub", "published with readback", gms=url, aspects=len(verified))
    if not all(c.passed for c in checks):
        raise ValueError("a RAG quality assertion failed (published to DataHub as FAILURE)")
    corpus_dataset = governance.corpus_urn(documents["source_location"], documents["corpus_revision"])
    visibility = governance.verify_index_visibility(adapter.graphql, feast_dataset=feast_dataset,
                                                    corpus_dataset=corpus_dataset)
    log("datahub", "search and lineage index", **visibility)
    if visibility["status"] != "VISIBLE":
        raise ValueError(f"DataHub stored the metadata but its search/lineage index does not show it: "
                         f"{visibility['checks']}; check OpenSearch health and disk (read-only indices)")
    return {**summary, "status": "PUBLISHED_AND_READ_BACK", "gms_url": url, "verified": verified,
            "index_visibility": visibility}


STAGES = {"discover": ("documents.json", discover), "chunk": ("chunks.json", chunk),
          "embed": ("embeddings.json", embed), "publish": ("publication.json", publish),
          "validate": ("validation.json", validate), "datahub": ("datahub.json", datahub)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=sorted(STAGES))
    parser.add_argument("--run-label", default="")
    parser.add_argument("--airflow-run-id", default="")
    parser.add_argument("--source-location", default="data/llm_agent/rag/fraud-policy-v1")
    parser.add_argument("--online-store", choices=("redis", "sqlite"), default="redis")
    parser.add_argument("--datahub-mode", choices=("publish", "plan_only"), default="publish")
    parser.add_argument("--evaluation", default="data/llm_agent/rag/fraud-policy-v1/evaluation.json")
    parser.add_argument("--min-recall", type=float, default=1.0)
    args = parser.parse_args(argv)
    args.run_id = derive_run_id(run_label=args.run_label, airflow_run_id=args.airflow_run_id)
    directory = run_dir(args.run_id)
    output, function = STAGES[args.stage]
    if (directory / output).exists():
        raise FileExistsError(f"{output} already exists for run {args.run_id}; use a new run id")
    log(args.stage, "start", run_dir=str(directory), env_file_keys=load_env_file())
    payload = function(args, directory)
    write_once(directory / output, {**payload, "run_id": args.run_id,
                                    "completed_at": datetime.now(timezone.utc).isoformat()})
    log(args.stage, "done", artifact=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
