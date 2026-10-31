# syntax=docker/dockerfile:1.7
ARG BASELINE_IMAGE=python:3.13.9-bookworm@sha256:f7c7710623c1234f7ebdf4cbb9d1302dc67a552f7a3daa3e655e880df0b9c343
ARG OPTIMIZED_IMAGE=python:3.13.9-slim-bookworm@sha256:8cdd496996889eef0a8a7921c152c0a4a9f18a26c7a5e7257d3c90b431891894

FROM ${BASELINE_IMAGE} AS baseline
WORKDIR /app
COPY data_platform/metadata/datahub/lineage/cloud_lineage.py /app/cloud_lineage.py
ENTRYPOINT ["python", "/app/cloud_lineage.py"]

FROM ${OPTIMIZED_IMAGE} AS build
WORKDIR /build
COPY data_platform/metadata/datahub/lineage/cloud_lineage.py /build/cloud_lineage.py
RUN python -m compileall -q /build/cloud_lineage.py \
    && python /build/cloud_lineage.py --output /tmp/lineage-plan.json \
    && python -c "import json; p=json.load(open('/tmp/lineage-plan.json')); assert (p['flow_count'],p['job_count'],p['dataset_count']) == (3,4,32)"

FROM ${OPTIMIZED_IMAGE} AS optimized
RUN groupadd --system --gid 65532 publisher \
    && useradd --system --uid 65532 --gid 65532 --no-create-home publisher
WORKDIR /app
COPY --from=build --chown=65532:65532 /build/cloud_lineage.py /app/cloud_lineage.py
USER 65532:65532
ENTRYPOINT ["python", "/app/cloud_lineage.py"]
