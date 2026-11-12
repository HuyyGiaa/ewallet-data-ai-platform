"""Thin adapter over the Docker and Docker Compose command lines.

Every call goes through one injectable ``runner`` so the measurement logic is
tested without a Docker daemon. Commands are passed as argument lists, never
through a shell.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import COMPOSE_PROJECT
from .variants import Variant

Runner = Callable[..., subprocess.CompletedProcess]


class DockerError(RuntimeError):
    """A Docker command failed."""


@dataclass(frozen=True)
class Mount:
    """A read-only bind mount."""

    source: Path
    target: str

    def argument(self) -> str:
        return f"type=bind,source={self.source.resolve()},target={self.target},readonly"


@dataclass
class DockerCli:
    """Docker operations used by the benchmark, all scoped to explicit tags."""

    context: Path
    runner: Runner = subprocess.run

    def _run(self, args: Sequence[str], *, check: bool = True, log: Path | None = None) -> subprocess.CompletedProcess:
        completed = self.runner(list(args), text=True, capture_output=True, check=False)
        if log is not None:
            log.write_text(f"$ {' '.join(args)}\n{completed.stdout}{completed.stderr}"
                           f"\n[exit code {completed.returncode}]\n", encoding="utf-8")
        if check and completed.returncode != 0:
            raise DockerError(f"{' '.join(args[:4])} failed ({completed.returncode}): {completed.stderr.strip()[:500]}")
        return completed

    def versions(self) -> dict[str, Any]:
        """Docker engine, client, Buildx and Compose versions plus host architecture."""

        info = json.loads(self._run(["docker", "version", "--format", "{{json .}}"]).stdout)
        return {
            "engine": info.get("Server", {}).get("Version"),
            "client": info.get("Client", {}).get("Version"),
            "architecture": info.get("Server", {}).get("Arch"),
            "os": info.get("Server", {}).get("Os"),
            "buildx": self._run(["docker", "buildx", "version"]).stdout.strip(),
            "compose": self._run(["docker", "compose", "version", "--short"]).stdout.strip(),
        }

    def pull(self, reference: str) -> None:
        """Pull a digest-pinned base image so build timings exclude the download."""

        self._run(["docker", "pull", "--quiet", reference])

    def inspect(self, reference: str) -> dict[str, Any]:
        """Return ``docker image inspect`` of one image."""

        return json.loads(self._run(["docker", "image", "inspect", reference]).stdout)[0]

    def disk_usage(self, reference: str) -> str:
        """Unpacked disk usage as ``docker image ls`` reports it (human-readable, SI units)."""

        repository, tag = reference.rsplit(":", 1)
        lines = self._run(["docker", "image", "ls", "--format", "{{json .}}", reference]).stdout.splitlines()
        for line in lines:
            item = json.loads(line)
            if item.get("Repository") == repository and item.get("Tag") == tag:
                return str(item.get("Size", ""))
        return ""

    def history(self, reference: str) -> list[dict[str, Any]]:
        """Return the image history, one dictionary per layer instruction."""

        lines = self._run(["docker", "image", "history", "--no-trunc", "--format", "{{json .}}", reference]).stdout
        return [json.loads(line) for line in lines.splitlines() if line.strip()]

    def build(self, item: Variant, tag: str, *, log: Path) -> float:
        """Build one variant without the layer cache; return wall-clock seconds."""

        args = ["docker", "build", "--no-cache", "--pull=false", "--progress=plain",
                "--file", str(item.dockerfile), "--tag", tag]
        for key, value in sorted(item.build_args.items()):
            args += ["--build-arg", f"{key}={value}"]
        if item.target:
            args += ["--target", item.target]
        args.append(str(self.context))
        started = time.perf_counter()
        self._run(args, log=log)
        return time.perf_counter() - started

    def run(self, tag: str, arguments: Sequence[str], mounts: Sequence[Mount]) -> tuple[int, str, str, float]:
        """Run the image once with no network, no capabilities and a read-only root."""

        args = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "256m"]
        for mount in mounts:
            args += ["--mount", mount.argument()]
        args += [tag, *arguments]
        started = time.perf_counter()
        completed = self._run(args, check=False)
        return completed.returncode, completed.stdout, completed.stderr, time.perf_counter() - started

    def probe(self, tag: str, command: Sequence[str]) -> str:
        """Run a diagnostic command (``id``, ``sh -c``) inside the image; return its output."""

        args = ["docker", "run", "--rm", "--network", "none", "--entrypoint", command[0], tag, *command[1:]]
        return self._run(args, check=False).stdout.strip()

    def compose(self, compose_file: Path, *arguments: str, log: Path | None = None,
                check: bool = True) -> subprocess.CompletedProcess:
        """Run ``docker compose`` on the benchmark project with the edai1 profile."""

        args = ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(compose_file), "--profile", "edai1", *arguments]
        return self._run(args, check=check, log=log)

    def project_containers(self) -> list[str]:
        """Containers left behind by the benchmark's Compose project."""

        output = self._run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                            "--format", "{{.Names}}"]).stdout
        return [line for line in output.splitlines() if line.strip()]

    def remove_image(self, tag: str) -> None:
        """Remove one image created by this benchmark (explicit, tag-scoped)."""

        self._run(["docker", "image", "rm", tag])
