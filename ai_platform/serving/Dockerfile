# KServe predictor with the registered model baked in (model-inside-image).
# Build context is assembled by ops/mlops/bake_serving_image.py.
FROM python:3.10.22-slim-bookworm@sha256:db6ee5170ebce0f4d165af242e8e77addc08bd9ffc37e8b1815752cf027aa4f8

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    PATH=/app/ai_platform/serving/.venv/bin:$PATH

WORKDIR /app
COPY ai_platform/serving/pyproject.toml ai_platform/serving/uv.lock ./ai_platform/serving/
RUN uv sync --project ai_platform/serving --locked --no-dev \
    --no-install-project --python /usr/local/bin/python
RUN python -c "import kserve, mlflow, sklearn, numpy; assert numpy.__version__ == '2.2.6' and sklearn.__version__ == '1.7.2'; print('SERVING_IMAGE_IMPORT_PASS')"

COPY ai_platform ./ai_platform
COPY models/fraud /models/fraud

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "-m", "ai_platform.serving.fraud_model_server", "--model-dir", "/models/fraud"]
