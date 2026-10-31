FROM python:3.12.12-alpine3.22@sha256:848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32 AS build

WORKDIR /workspace
COPY ai_platform/llm_agent ai_platform/llm_agent
COPY ops/llm_agent ops/llm_agent
RUN python -m compileall -q ai_platform/llm_agent ops/llm_agent

# Phase D MCP tool servers (feature-rag-mcp, drift-mcp): `--target mcp-server`.
# glibc base because Feast/pyarrow ship manylinux wheels only. Dependencies come
# from the reviewed uv.lock (pyproject.toml beside this file): --locked fails if
# the lock no longer matches pyproject.toml and never re-resolves, uv checks every artifact hash from the lock, --no-build refuses
# sdists, and the dev group (MCP SDK, tests only) is excluded.
FROM python:3.12.12-slim-trixie@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c AS mcp-deps

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /opt/mcp-project
COPY infra/docker/llm-agent-tooling/pyproject.toml infra/docker/llm-agent-tooling/uv.lock ./
RUN uv sync --locked --no-dev --extra feast --no-install-project --no-build --no-cache \
       --python /usr/local/bin/python3.12

FROM python:3.12.12-slim-trixie@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c AS mcp-server

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EWALLET_RAG_MANIFEST=/workspace/data/llm_agent/rag/fraud-policy-v1/manifest.json
WORKDIR /workspace
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=mcp-deps /opt/venv /opt/venv
COPY ai_platform/__init__.py ai_platform/__init__.py
COPY ai_platform/inference ai_platform/inference
COPY ai_platform/llm_agent ai_platform/llm_agent
COPY ai_platform/ml/__init__.py ai_platform/ml/__init__.py
COPY ai_platform/ml/training/__init__.py ai_platform/ml/training/contract.py ai_platform/ml/training/derived.py ai_platform/ml/training/
COPY ai_platform/features/__init__.py ai_platform/features/__init__.py
COPY ai_platform/features/feast/__init__.py ai_platform/features/feast/schema.py ai_platform/features/feast/
COPY data/llm_agent/rag/fraud-policy-v1 data/llm_agent/rag/fraud-policy-v1
USER 10001:10001
EXPOSE 8080

FROM python:3.12.12-alpine3.22@sha256:848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /workspace
RUN addgroup -S app && adduser -S -G app -u 10001 app
COPY --from=build --chown=app:app /workspace /workspace
COPY --chown=app:app configs/llm_agent configs/llm_agent
COPY --chown=app:app infra/helm/llm-agent-platform infra/helm/llm-agent-platform
COPY --chown=app:app infra/kubernetes/kagent-pilot/versions.env infra/kubernetes/kagent-pilot/versions.env
USER 10001:10001
ENTRYPOINT ["python", "-m", "ops.llm_agent.preflight"]
