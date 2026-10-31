FROM ghcr.io/mlflow/mlflow:v3.17.1

WORKDIR /opt/mlflow-runtime
RUN python -m pip install --no-cache-dir uv==0.9.5
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
ENV PATH=/opt/mlflow-runtime/.venv/bin:$PATH
