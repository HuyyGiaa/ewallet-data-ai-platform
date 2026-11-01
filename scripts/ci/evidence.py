"""Execution-bound CI evidence and failure classification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable


def classify_failure(stage: str, exit_code: int, tool_state: str) -> str:
    if exit_code == 0:
        return "NONE"
    if tool_state == "missing" or exit_code == 127:
        return "MISSING_RUNTIME_DEPENDENCY"
    if "security" in stage or "scan" in stage:
        return "SECURITY_FAILURE"
    if stage.startswith(("deploy", "registry", "helm", "kubectl", "docker-build")):
        return "INFRASTRUCTURE_FAILURE"
    if stage.startswith(("checkout", "provenance", "workspace")):
        return "ENVIRONMENT_FAILURE"
    return "SOURCE_REGRESSION"


class EvidenceWriter:
    def __init__(self, directory: Path, metadata: dict[str, Any]) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.metadata = dict(metadata)

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write_text(self, relative: str, content: str) -> Path:
        path = self.directory / relative
        self._atomic_write(path, content)
        return path

    def write_json(self, relative: str, data: Any) -> Path:
        return self.write_text(relative, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def write_lock_hashes(self, root: Path, projects: Iterable[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for project in sorted(set(projects)):
            lock = root / project / "uv.lock"
            hashes[project] = hashlib.sha256(lock.read_bytes()).hexdigest()
        self.write_json("lock-hashes.json", hashes)
        return hashes

    def finalize(self, *, status: str, results: list[dict[str, Any]]) -> None:
        manifest = {
            "schema_version": 1,
            **self.metadata,
            "status": status,
            "results": results,
            "verification": {
                "source_ready": True,
                "static_validation_pass": status == "PASS",
                "local_ci_verified": status == "PASS",
                "jenkins_runtime_verified": False,
                "cd_runtime_verified": False,
            },
        }
        self.write_json("manifest.json", manifest)
        lines: list[str] = []
        for path in sorted(self.directory.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                lines.append(f"{digest}  {path.relative_to(self.directory).as_posix()}")
        self.write_text("SHA256SUMS", "\n".join(lines) + "\n")
