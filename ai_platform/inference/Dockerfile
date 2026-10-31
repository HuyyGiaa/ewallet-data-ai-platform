# Fraud API (Block B): Feast Online lookup -> 41-feature assembly -> KServe V2.
FROM python:3.10.22-slim-bookworm@sha256:db6ee5170ebce0f4d165af242e8e77addc08bd9ffc37e8b1815752cf027aa4f8

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    EWALLET_FEAST_REPO_PATH=/app/ai_platform/features/feast/online_repo \
    PATH=/app/ai_platform/inference/.venv/bin:$PATH

WORKDIR /app
COPY ai_platform/inference/pyproject.toml ai_platform/inference/uv.lock ./ai_platform/inference/
RUN uv sync --project ai_platform/inference --locked --no-dev \
    --no-install-project --python /usr/local/bin/python
RUN python -c "import feast, fastapi, redis, opentelemetry.sdk; assert feast.__version__ == '0.66.0'; print('FRAUD_API_IMAGE_IMPORT_PASS')"

COPY ai_platform ./ai_platform

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "-m", "ai_platform.inference.server"]
