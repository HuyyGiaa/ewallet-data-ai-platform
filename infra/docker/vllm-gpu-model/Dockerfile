# Both A/B models in one image for one L4, offline, at pinned revisions. Same vLLM
# version as the CPU image. Build context: model-0.6b/, model-1.7b/, SHA256SUMS-0.6b and
# SHA256SUMS-1.7b (scripts/experiments/build_vllm_gpu_image.sh). The upstream image has
# not been through the Trivy remediation of infra/docker/vllm-cpu-model: the owner's scan
# decides whether it may be pushed. Its CUDA 13 runtime needs a driver checked on the node.
FROM docker.io/vllm/vllm-openai:v0.32.0@sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34
ARG MODEL_06B=/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca
ARG MODEL_17B=/models/Qwen3-1.7B/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e
COPY model-0.6b/ ${MODEL_06B}/
COPY SHA256SUMS-0.6b ${MODEL_06B}/SHA256SUMS
COPY model-1.7b/ ${MODEL_17B}/
COPY SHA256SUMS-1.7b ${MODEL_17B}/SHA256SUMS
RUN chmod -R a=rX,u-w,g-w /models \
 && cd ${MODEL_06B} && sha256sum --check --strict --quiet SHA256SUMS \
 && cd ${MODEL_17B} && sha256sum --check --strict --quiet SHA256SUMS
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
