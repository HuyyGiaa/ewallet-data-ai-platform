# Model-inside-image vLLM CPU: the pinned upstream image, its Trivy
# HIGH/CRITICAL findings fixed in place, plus Qwen3-0.6B at the pinned revision.
# Build context (scripts/llm_agent/build_vllm_model_image.sh): pyproject.toml,
# uv.lock, remediate.py, SHA256SUMS and model/ with the staged files.
# Served offline (values-gke-cpu.yaml, mode image).
#
# Deviations from a plain `uv sync --locked`, recorded and tested
# (tests/llm_agent/test_vllm_model_image.py):
# - the upstream vLLM venv is not re-resolved (syncing it to this lock would delete every
#   vLLM dependency outside this lock); only this project's locked export is
#   installed into it, hash-checked and without dependencies;
# - libbrotli1 is an apt upgrade to a pinned Ubuntu version;
# - setuptools 78.1.1 (pinned by vLLM) gets the vendored jaraco.context and
#   wheel of the locked setuptools 82.0.1 wheel; pip is removed.
FROM docker.io/vllm/vllm-openai-cpu:v0.32.0-x86_64@sha256:827ef38187f9b52f2bb838695ef815ec5b65f4bf3e4c061b002b2b0a19cb0701 AS remediated
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /tmp/uv-pinned/uv
COPY pyproject.toml uv.lock remediate.py /tmp/remediation/
ENV UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade libbrotli1=1.0.9-2ubuntu0.1 \
 && rm -rf /var/lib/apt/lists/*
RUN cd /tmp/remediation \
 && /tmp/uv-pinned/uv lock --check --python /opt/venv/bin/python3 \
 && /tmp/uv-pinned/uv export --locked --no-emit-project --format requirements-txt -o requirements.txt \
 && /tmp/uv-pinned/uv pip install --python /opt/venv/bin/python3 --require-hashes --no-deps -r requirements.txt \
 && /opt/venv/bin/python3 remediate.py uv.lock \
 && rm -rf /tmp/remediation /tmp/uv-pinned /root/.cache/uv

FROM remediated
ARG MODEL_DIR=/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca
COPY --chmod=0444 model/ ${MODEL_DIR}/
COPY --chmod=0444 SHA256SUMS ${MODEL_DIR}/SHA256SUMS
RUN cd ${MODEL_DIR} && sha256sum --check --strict --quiet SHA256SUMS
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
