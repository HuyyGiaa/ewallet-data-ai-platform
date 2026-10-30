FROM python:3.12.12-alpine3.22@sha256:848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32 AS build

WORKDIR /workspace
COPY ai_platform/llm_agent ai_platform/llm_agent
COPY ops/llm_agent ops/llm_agent
RUN python -m compileall -q ai_platform/llm_agent ops/llm_agent

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
