"""The four image variants and the inputs every variant runs on."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import IMAGE_REPOSITORY

FULL_BASE = "python:3.13.9-bookworm@sha256:f7c7710623c1234f7ebdf4cbb9d1302dc67a552f7a3daa3e655e880df0b9c343"
SLIM_BASE = "python:3.13.9-slim-bookworm@sha256:8cdd496996889eef0a8a7921c152c0a4a9f18a26c7a5e7257d3c90b431891894"
IMAGE_DIR = Path("images/edai1-campaign-verifier")
EVIDENCE_DIR = Path("docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456")
SPEC_FILE = IMAGE_DIR / "workload-5gb.json"
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{2,40}$")


@dataclass(frozen=True)
class Variant:
    """One image build of the matrix.

    Attributes:
        name: Stable variant name used in tags and reports.
        dockerfile: Dockerfile path relative to the repository root.
        build_args: Build arguments; they only select the base image.
        target: Build target, or None for a single-stage file.
        base_image: The runtime base image reference (digest-pinned).
        packaging: ``single-stage-full-context`` or ``multi-stage-minimal-payload``.
        role: ``baseline``, ``optimized`` or ``control``.
    """

    name: str
    dockerfile: Path
    build_args: Mapping[str, str]
    target: str | None
    base_image: str
    packaging: str
    role: str

    def tag(self, run_id: str) -> str:
        """Image tag for one benchmark run."""

        if not _RUN_ID.fullmatch(run_id):
            raise ValueError(f"unsafe run id {run_id!r}")
        return f"{IMAGE_REPOSITORY}:{self.name}-{run_id}"


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline", IMAGE_DIR / "Dockerfile.baseline", {"BASE_IMAGE": FULL_BASE}, None,
            FULL_BASE, "single-stage-full-context", "baseline"),
    Variant("baseline-slim", IMAGE_DIR / "Dockerfile.baseline", {"BASE_IMAGE": SLIM_BASE}, None,
            SLIM_BASE, "single-stage-full-context", "control"),
    Variant("optimized-full", IMAGE_DIR / "Dockerfile.optimized",
            {"BUILD_IMAGE": FULL_BASE, "RUNTIME_IMAGE": FULL_BASE}, "runtime",
            FULL_BASE, "multi-stage-minimal-payload", "control"),
    Variant("optimized", IMAGE_DIR / "Dockerfile.optimized",
            {"BUILD_IMAGE": SLIM_BASE, "RUNTIME_IMAGE": SLIM_BASE}, "runtime",
            SLIM_BASE, "multi-stage-minimal-payload", "optimized"),
)


def variant(name: str) -> Variant:
    """Return the variant called ``name``."""

    for item in VARIANTS:
        if item.name == name:
            return item
    raise KeyError(name)
