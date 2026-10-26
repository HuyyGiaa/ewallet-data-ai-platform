FROM python:3.10.19-slim-bookworm

WORKDIR /workspace
COPY ai_platform/ml/requirements.txt /tmp/ml-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ml-requirements.txt

COPY ai_platform /workspace/ai_platform
ENV PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1
USER 65532:65532

ENTRYPOINT ["python", "-m", "ai_platform.ml.pipelines.runtime"]
