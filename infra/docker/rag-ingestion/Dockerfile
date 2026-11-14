# RAG ingestion task runtime (rag_fraud_policy_ingestion DAG stages, ops.llm_agent.rag_ingest).
# Build context: repository root. Dependencies come only from the reviewed
# uv.lock beside this file (--locked never re-resolves; uv checks every hash).
# The embedding model is fetched at build time at the manifest's immutable
# revision and read offline at runtime, so the pod needs no Internet egress.
FROM python:3.10.19-slim-trixie@sha256:ed9e9e63d6b7c71c8f80eae61ab889d3912ca097a8206dca52874457be507cf1 AS deps

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /opt/rag-project
COPY infra/docker/rag-ingestion/pyproject.toml infra/docker/rag-ingestion/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache --python /usr/local/bin/python3.10

FROM deps AS rag-model
ARG RAG_MODEL_ID=sentence-transformers/all-MiniLM-L6-v2
ARG RAG_MODEL_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41
RUN /opt/venv/bin/python -c "import sys; from huggingface_hub import hf_hub_download; \
[hf_hub_download(repo_id=sys.argv[1], filename=f, revision=sys.argv[2], cache_dir='/opt/hf-cache') \
 for f in ('modules.json', '1_Pooling/config.json', 'tokenizer.json', 'onnx/model.onnx')]" \
    "$RAG_MODEL_ID" "$RAG_MODEL_REVISION"

FROM python:3.10.19-slim-trixie@sha256:ed9e9e63d6b7c71c8f80eae61ab889d3912ca097a8206dca52874457be507cf1 AS runtime

ARG EWALLET_RELEASE_SHA=unknown
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EWALLET_RAG_MODEL_CACHE=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    EWALLET_RELEASE_SHA=${EWALLET_RELEASE_SHA}
LABEL org.opencontainers.image.revision=${EWALLET_RELEASE_SHA} \
      org.opencontainers.image.title="E-Wallet RAG ingestion runtime"
WORKDIR /workspace
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=deps /opt/venv /opt/venv
COPY --from=rag-model /opt/hf-cache /opt/hf-cache
COPY ai_platform/__init__.py ai_platform/__init__.py
COPY ai_platform/inference ai_platform/inference
COPY ai_platform/llm_agent ai_platform/llm_agent
COPY ai_platform/features/__init__.py ai_platform/features/__init__.py
COPY ai_platform/features/feast ai_platform/features/feast
COPY data_platform/__init__.py data_platform/__init__.py
COPY data_platform/metadata data_platform/metadata
COPY ops/__init__.py ops/__init__.py
COPY ops/llm_agent ops/llm_agent
COPY data/llm_agent/rag data/llm_agent/rag
COPY infra/docker/rag-ingestion/uv.lock infra/docker/rag-ingestion/uv.lock
COPY scripts/ci/rag_smoke.sh scripts/ci/rag_smoke.sh
USER 10001:10001
ENTRYPOINT ["/bin/bash", "scripts/ci/rag_smoke.sh"]
