FROM python:3.10.19-slim-bookworm

WORKDIR /workspace
RUN pip install --no-cache-dir uv==0.9.5
COPY ai_platform/ml/pyproject.toml ai_platform/ml/uv.lock /opt/f9-runtime/
RUN cd /opt/f9-runtime && uv sync --locked --no-dev

COPY ai_platform /workspace/ai_platform
# modeling.data imports the shared Spark-profile lineage guard.
COPY data_platform/__init__.py /workspace/data_platform/__init__.py
COPY data_platform/config /workspace/data_platform/config
ENV PATH=/opt/f9-runtime/.venv/bin:$PATH \
    PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1
RUN python -m ai_platform.ml.pipelines.runtime --help > /dev/null && echo F9_RUNTIME_IMPORT_PASS
USER 65532:65532

ENTRYPOINT ["python", "-m", "ai_platform.ml.pipelines.runtime"]
