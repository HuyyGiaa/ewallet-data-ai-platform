from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_catalog_and_dockerfile_preserve_comparable_contract():
    catalog = (ROOT / "images/catalog.yaml").read_text()
    dockerfile = (ROOT / "images/datahub-publisher/Dockerfile").read_text()
    assert "expected_flows: 3" in catalog
    assert "expected_jobs: 4" in catalog
    assert "python:3.13.9-bookworm@sha256:" in dockerfile
    assert "python:3.13.9-slim-bookworm@sha256:" in dockerfile
    assert "FROM ${OPTIMIZED_IMAGE} AS build" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert dockerfile.count("COPY data_platform/metadata/datahub/lineage/cloud_lineage.py") == 2
