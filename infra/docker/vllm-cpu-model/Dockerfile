# Model-inside-image vLLM CPU: the pinned upstream image, its Trivy
# HIGH/CRITICAL findings fixed in place (remediate.py, docs/llm_agent/
# gke_cpu_runtime.md), plus Qwen3-0.6B at the pinned revision. Build context
# (scripts/llm_agent/build_vllm_model_image.sh): remediate.py, SHA256SUMS and
# model/ with the staged files. Served offline (values-gke-cpu.yaml, mode image).
FROM docker.io/vllm/vllm-openai-cpu:v0.32.0-x86_64@sha256:827ef38187f9b52f2bb838695ef815ec5b65f4bf3e4c061b002b2b0a19cb0701 AS remediated
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade libbrotli1=1.0.9-2ubuntu0.1 \
 && rm -rf /var/lib/apt/lists/*
COPY remediate.py /tmp/remediate.py
RUN python3 /tmp/remediate.py && rm /tmp/remediate.py

FROM remediated
ARG MODEL_DIR=/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca
COPY --chmod=0444 model/ ${MODEL_DIR}/
COPY --chmod=0444 SHA256SUMS ${MODEL_DIR}/SHA256SUMS
RUN cd ${MODEL_DIR} && sha256sum --check --strict --quiet SHA256SUMS
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
