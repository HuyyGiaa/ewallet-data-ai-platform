#!/usr/bin/env bash
# Run every rag_fraud_policy stage once against an isolated Feast/Redis target
# and print the stage artifacts the CD verifier checks. The run directory and
# publication root live on an emptyDir; production Redis DB0/DB1 is never named.
set -euo pipefail

run_label=${1:?usage: rag_smoke.sh <run-label>}
: "${EWALLET_RAG_FEAST_REGISTRY:?}" "${EWALLET_REDIS_CONNECTION_STRING:?}" "${EWALLET_RAG_PUBLICATION_BASE:?}"
case "$EWALLET_REDIS_CONNECTION_STRING" in
  *feature-store*|*redis.feature-store*)
    echo "refusing a production Feast Online Redis target" >&2
    exit 3
    ;;
esac

for stage in discover chunk embed publish validate datahub; do
  extra=()
  [[ $stage == datahub ]] && extra=(--datahub-mode plan_only)
  python -m ops.llm_agent.rag_ingest "$stage" --run-label "$run_label" "${extra[@]}"
done

run_dir="docs/evidence/llm_agent/rag_${run_label}"
python - "$run_dir" <<'PY'
import json, sys
from pathlib import Path

run = Path(sys.argv[1])
read = lambda name: json.loads((run / name).read_text(encoding="utf-8"))
chunks, embeddings, publication, validation = (
    read("chunks.json"), read("embeddings.json"), read("publication.json"), read("validation.json"))
summary = {
    "release_sha": __import__("os").environ.get("EWALLET_RELEASE_SHA"),
    "corpus_revision": chunks["corpus_revision"],
    "chunk_count": chunks["chunk_count"],
    "embedding_model": embeddings.get("model_id"),
    "embedding_revision": embeddings.get("model_revision"),
    "embedding_dimensions": embeddings.get("dimensions"),
    "published_rows": publication["row_count"],
    "publication_identity": publication["publication_identity"],
    "feast_target": publication["feast"],
    "validation": {key: validation[key] for key in validation if key not in {"queries", "results"}},
}
print("RAG_SMOKE_SUMMARY " + json.dumps(summary, sort_keys=True))
PY
