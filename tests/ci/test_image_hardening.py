"""Release images drop the packaging tools they never run (rag-pipeline #10 and streaming-sinks Trivy findings).

Batch 2B-1 build #10 was VULNERABLE with 11 HIGH/CRITICAL findings. Eight of them were packaging
tools, not the task's own dependencies: the base image's system pip/setuptools/wheel (wheel x2,
jaraco.context) and the msgpack/urllib3/pkg_resources vendored inside the venv's pip (declared by
acryl-datahub, never run). The image removes them; the locked runtime packages are unchanged and
no finding is excepted.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/rag-ingestion/Dockerfile"


def _stage(text: str, name: str) -> str:
    start = text.index(f" AS {name}\n")
    following = text.find("\nFROM ", start)
    return text[start:] if following == -1 else text[start:following]


def test_venv_keeps_the_locked_sync_and_drops_only_pip() -> None:
    deps = _stage(DOCKERFILE.read_text(encoding="utf-8"), "deps")
    sync = deps.index("uv sync --locked --no-dev --no-install-project --no-cache")
    uninstall = deps.index("uv pip uninstall --python /opt/venv/bin/python pip\n")
    assert sync < uninstall
    assert "upgrade" not in deps and "uv lock" not in deps


def test_runtime_removes_the_base_image_packaging_tools() -> None:
    runtime = _stage(DOCKERFILE.read_text(encoding="utf-8"), "runtime")
    assert "/usr/local/bin/python3.11 -m pip uninstall --yes --no-input pip setuptools wheel" in runtime
    assert "rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11 /usr/local/bin/wheel" in runtime
    assert runtime.index("pip uninstall") < runtime.index("COPY --from=deps /opt/venv /opt/venv")
    assert runtime.index("pip uninstall") < runtime.index("USER 10001:10001")


def test_the_scan_gate_is_not_relaxed() -> None:
    exceptions = json.loads((ROOT / "jenkins/security/vulnerability-exceptions.json").read_text(encoding="utf-8"))
    assert exceptions["exceptions"] == []
    scan = (ROOT / "scripts/ci/image_scan.py").read_text(encoding="utf-8")
    assert 'SEVERITIES = ("HIGH", "CRITICAL")' in scan or "SEVERITIES = ('HIGH', 'CRITICAL')" in scan


def test_streaming_sinks_take_security_updates_and_drop_the_base_packaging_tools() -> None:
    # streaming-sinks (row 36) rescanned on 2026-11-20 with Trivy 0.75.0: apt/OpenSSL (fixed in Debian
    # updates) and the base image's wheel/jaraco.context; only feast stays (an owner decision, as for RAG).
    text = (ROOT / "infra/docker/streaming-sinks/Dockerfile").read_text(encoding="utf-8")
    hardening = text.index("apt-get -y --no-install-recommends upgrade")
    assert text.index("/usr/local/bin/python3.10 -m pip uninstall --yes --no-input pip setuptools wheel") > hardening
    assert hardening < text.index("uv sync --locked --no-dev --no-install-project")
    assert text.index("USER 65532:65532") > hardening


def test_flink_image_takes_ubuntu_updates_and_the_patched_setuptools() -> None:
    # flink-block-a rescanned 2026-11-20: 29 -> 9 findings after the Ubuntu upgrade and setuptools 80.10.2
    # (vendored wheel 0.46.3, jaraco.context 6.1.0; still ships the pkg_resources PyFlink 1.19 imports).
    # The 9 left are pinned by Flink 1.19.1 / apache-beam 2.48 / Python 3.10 and await an owner decision.
    text = (ROOT / "infra/docker/flink-benchmark/Dockerfile").read_text(encoding="utf-8")
    assert text.index("apt-get -y --no-install-recommends upgrade") < text.index("uv sync --locked")
    pyproject = (ROOT / "infra/docker/flink-benchmark/pyproject.toml").read_text(encoding="utf-8")
    assert '"setuptools==80.10.2"' in pyproject and '"apache-flink==1.19.1"' in pyproject


def test_the_legacy_rag_ignorefile_excepts_nothing() -> None:
    lines = (ROOT / "infra/docker/rag-ingestion/trivyignore").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.strip() and not line.lstrip().startswith("#")] == []
