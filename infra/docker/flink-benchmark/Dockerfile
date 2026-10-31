# One image for the Flink JobManager/TaskManager, the replay generator and the
# isolated output reader. Build context: repository root.
#
# JVM side (not covered by uv.lock): Flink 1.19.1 / Scala 2.12 / Java 11 pinned
# by digest, plus the committed flink-sql-connector-kafka-3.3.0-1.19.jar.
# Python side: CPython 3.10 from Ubuntu 22.04 (the Flink base) and the reviewed
# uv.lock beside this file. --locked fails if the lock no longer matches
# pyproject.toml and never re-resolves; uv verifies every artifact hash. Six
# locked pure-Python sdists (apache-flink-libraries, avro-python3, crcmod, dill,
# docopt, hdfs) are built from their hashed sdists with the build backend pinned
# by [tool.uv] build-constraint-dependencies, so --no-build cannot be used.
FROM flink:1.19.1-scala_2.12-java11@sha256:ab8afa8fc74fb7138499be1ead80a4057669785efc7c43cc6944aa22a50c0339

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.10 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /opt/uv-project
COPY infra/docker/flink-benchmark/pyproject.toml infra/docker/flink-benchmark/uv.lock ./
RUN uv sync --locked --no-dev --extra flink --no-install-project --python /usr/bin/python3.10 \
    && /opt/venv/bin/python -c "import pyflink, confluent_kafka, yaml; print('pyflink ok')"

# PyFlink's Java bridge ships in opt/ and must be on the cluster classpath.
RUN cp /opt/flink/opt/flink-python-1.19.1.jar /opt/flink/lib/ \
    && mkdir -p /flink-data && chown flink:flink /flink-data

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/ewallet \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/ewallet
COPY ai_platform/__init__.py ai_platform/__init__.py
COPY ai_platform/features/__init__.py ai_platform/features/__init__.py
COPY ai_platform/features/feast/__init__.py ai_platform/features/feast/schema.py ai_platform/features/feast/
COPY data_platform/__init__.py data_platform/__init__.py
COPY data_platform/generation/__init__.py data_platform/generation/__init__.py
COPY data_platform/generation/src/__init__.py data_platform/generation/src/fintech_schema.py data_platform/generation/src/
COPY data_platform/processing/flink/jars/flink-sql-connector-kafka-3.3.0-1.19.jar data_platform/processing/flink/jars/
COPY data_platform/streaming data_platform/streaming
COPY configs/streaming configs/streaming
RUN python -m compileall -q data_platform/streaming \
    && python -c "from data_platform.streaming.config.settings import BASELINE, OPTIMIZED; print(BASELINE.config_sha256, OPTIMIZED.config_sha256)"
