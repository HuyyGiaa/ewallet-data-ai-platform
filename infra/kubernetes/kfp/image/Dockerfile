FROM python:3.10.19-slim-bookworm

WORKDIR /workspace
RUN pip install --no-cache-dir uv==0.9.5
COPY ai_platform/ml/pyproject.toml ai_platform/ml/uv.lock /opt/f9-runtime/
RUN cd /opt/f9-runtime && uv sync --frozen --no-dev

COPY ai_platform /workspace/ai_platform
ENV PATH=/opt/f9-runtime/.venv/bin:$PATH \
    PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1
USER 65532:65532

ENTRYPOINT ["python", "-m", "ai_platform.ml.pipelines.runtime"]
