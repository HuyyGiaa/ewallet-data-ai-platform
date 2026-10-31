"""Build a narrow Python archive for Dataproc jobs."""

from __future__ import annotations

import zipfile
from pathlib import Path


INCLUDED_ROOTS = ("data_platform", "ops")


def build_python_archive(
    repository_root: Path,
    destination: Path,
    *,
    included_roots: tuple[str, ...] = INCLUDED_ROOTS,
) -> Path:
    root = repository_root.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for package in included_roots:
            package_root = root / package
            if not package_root.is_dir():
                raise FileNotFoundError(f"Python archive root is missing: {package_root}")
            for path in sorted(package_root.rglob("*.py")):
                relative = path.relative_to(root)
                if "__pycache__" in relative.parts or any(
                    part.startswith(".") for part in relative.parts
                ):
                    continue
                bundle.write(path, relative.as_posix())
    temporary.replace(destination)
    return destination
