FROM python:3.12.12-slim-trixie@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c AS deps

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /opt/coordinator-project
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY infra/docker/g3-byo-coordinator/pyproject.toml infra/docker/g3-byo-coordinator/uv.lock ./
# The only Git dependency is kagent-core from the immutable upstream kagent
# v0.10.3 commit recorded in pyproject.toml and uv.lock.
RUN uv sync --locked --no-dev --no-install-project --no-cache \
      --python /usr/local/bin/python3.12

FROM python:3.12.12-slim-trixie@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
WORKDIR /workspace
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=deps /opt/venv /opt/venv
COPY ai_platform/__init__.py ai_platform/__init__.py
COPY ai_platform/inference ai_platform/inference
COPY ai_platform/llm_agent ai_platform/llm_agent
COPY ai_platform/features/__init__.py ai_platform/features/__init__.py
COPY ai_platform/features/feast/__init__.py ai_platform/features/feast/__init__.py
COPY ai_platform/features/feast/schema.py ai_platform/features/feast/schema.py
COPY ai_platform/ml/__init__.py ai_platform/ml/__init__.py
COPY ai_platform/ml/training ai_platform/ml/training
RUN python -m compileall -q ai_platform
USER 10001:10001
EXPOSE 8080
# Keep one locked G3 Python runtime while allowing the kagent BYO Agent CR to
# override only the module argument for the bounded Specialist image.
ENTRYPOINT ["python", "-m"]
CMD ["ai_platform.llm_agent.byo_coordinator"]
