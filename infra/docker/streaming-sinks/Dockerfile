# Block A sinks: the accepted-history writer, the Feast online writer and the
# consistency validator. Kept apart from the Flink image so Feast and GCS clients
# never enter the TaskManager environment. Build context: repository root.
FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370

ARG SOURCE_SHA
LABEL org.opencontainers.image.revision="${SOURCE_SHA}"

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /opt/uv-project
COPY infra/docker/streaming-sinks/pyproject.toml infra/docker/streaming-sinks/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/ewallet \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
WORKDIR /opt/ewallet
COPY ai_platform/__init__.py ai_platform/__init__.py
COPY ai_platform/features/__init__.py ai_platform/features/__init__.py
COPY ai_platform/features/feast ai_platform/features/feast
COPY data_platform/__init__.py data_platform/__init__.py
COPY data_platform/config data_platform/config
COPY data_platform/generation/__init__.py data_platform/generation/__init__.py
COPY data_platform/generation/src/__init__.py data_platform/generation/src/fintech_schema.py data_platform/generation/src/
COPY data_platform/streaming data_platform/streaming
COPY configs/streaming configs/streaming
USER 65532:65532
