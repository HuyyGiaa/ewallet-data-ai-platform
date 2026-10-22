"""Build a narrow Python archive for Dataproc jobs."""

from __future__ import annotations

import zipfile
from pathlib import Path


INCLUDED_ROOTS = ("data_platform", "ops")


def build_python_archive(repository_root: Path, destination: Path) -> Path:
    root = repository_root.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for package in INCLUDED_ROOTS:
            package_root = root / package
            for path in sorted(package_root.rglob("*.py")):
                relative = path.relative_to(root)
                if "__pycache__" in relative.parts or any(
                    part.startswith(".") for part in relative.parts
                ):
                    continue
                bundle.write(path, relative.as_posix())
    temporary.replace(destination)
    return destination
