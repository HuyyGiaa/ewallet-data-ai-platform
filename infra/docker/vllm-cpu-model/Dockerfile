# Model-inside-image vLLM CPU: the pinned upstream image plus Qwen3-0.6B at the
# pinned revision. Build context: the staged model directory (see
# scripts/llm_agent/build_vllm_model_image.sh), checked against SHA256SUMS here
# and again in the image. Served offline (values-gke-cpu.yaml, mode image).
FROM docker.io/vllm/vllm-openai-cpu:v0.32.0-x86_64@sha256:827ef38187f9b52f2bb838695ef815ec5b65f4bf3e4c061b002b2b0a19cb0701
ARG MODEL_DIR=/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca
COPY --chmod=0444 . ${MODEL_DIR}/
RUN cd ${MODEL_DIR} && sha256sum --check --strict --quiet SHA256SUMS
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
