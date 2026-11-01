# ML-2 Cloud Run materializer: the canonical Feast offline uv.lock (Python 3.10),
# the same dependency graph used by local materializer tests.
FROM python:3.10.22-slim-bookworm@sha256:db6ee5170ebce0f4d165af242e8e77addc08bd9ffc37e8b1815752cf027aa4f8

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/ai_platform/features/feast/.venv/bin:$PATH

WORKDIR /app
COPY ai_platform/features/feast/pyproject.toml ./ai_platform/features/feast/pyproject.toml
COPY ai_platform/features/feast/uv.lock ./ai_platform/features/feast/uv.lock
RUN uv sync --project ai_platform/features/feast --locked --no-dev \
    --no-install-project --python /usr/local/bin/python
RUN python -c "import sys, feast, dask, pyarrow; assert sys.version_info[:2] == (3, 10); assert feast.__version__ == '0.66.0'; print('ML2_IMAGE_IMPORT_PASS')"

COPY ai_platform ./ai_platform
COPY data_platform ./data_platform
COPY ops ./ops

ENTRYPOINT ["python", "-m", "ops.gcp.run_ml2_materialization"]
