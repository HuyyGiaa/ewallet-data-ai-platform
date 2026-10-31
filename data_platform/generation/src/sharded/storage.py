"""Minimal immutable object-store boundary backed by the installed gcloud CLI."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class ObjectStoreError(RuntimeError):
    """Raised when object-store state cannot be read or safely changed."""


@dataclass(frozen=True)
class ObjectMetadata:
    uri: str
    size: int
    md5_base64: str | None
    generation: str


class ObjectStore(Protocol):
    def exists(self, uri: str) -> bool: ...

    def read_text(self, uri: str) -> str: ...

    def write_text(self, text: str, uri: str, *, if_absent: bool) -> None: ...

    def write_file(self, path: Path, uri: str, *, if_absent: bool) -> None: ...

    def stat(self, uri: str) -> ObjectMetadata: ...


def file_md5_base64(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


class GcloudObjectStore:
    """Use gcloud  storage with ADC and generation preconditions."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._runner = runner

    def _run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = self._runner(
            command,
            check=False,
            text=True,
            capture_output=True,
        )
        if check and completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise ObjectStoreError(
                f"gcloud storage command failed ({completed.returncode}): {message}"
            )
        return completed

    def exists(self, uri: str) -> bool:
        completed = self._run(
            ["gcloud", "storage", "objects", "describe", uri, "--format=json"],
            check=False,
        )
        if completed.returncode == 0:
            return True
        message = f"{completed.stdout}\n{completed.stderr}".lower()
        if "not found" in message or "no urls matched" in message or "404" in message:
            return False
        raise ObjectStoreError(
            "could not determine whether GCS object exists: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )

    def read_text(self, uri: str) -> str:
        return self._run(["gcloud", "storage", "cat", uri]).stdout

    def write_text(self, text: str, uri: str, *, if_absent: bool) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            self.write_file(Path(handle.name), uri, if_absent=if_absent)

    def write_file(self, path: Path, uri: str, *, if_absent: bool) -> None:
        command = [
            "gcloud",
            "storage",
            "cp",
            str(path),
            uri,
            f"--content-md5={file_md5_base64(path)}",
            "--quiet",
        ]
        if if_absent:
            command.append("--if-generation-match=0")
        self._run(command)

    def stat(self, uri: str) -> ObjectMetadata:
        completed = self._run(
            ["gcloud", "storage", "objects", "describe", uri, "--format=json"]
        )
        try:
            value = json.loads(completed.stdout)
            return ObjectMetadata(
                uri=uri,
                size=int(value["size"]),
                md5_base64=value.get("md5_hash"),
                generation=str(value["generation"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObjectStoreError(f"invalid GCS object metadata for {uri}") from exc
