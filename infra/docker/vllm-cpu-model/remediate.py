"""Fix the Trivy HIGH/CRITICAL findings of the pinned vLLM CPU base, in place.

usage: python3 remediate.py uv.lock   (Dockerfile stage "remediated")

Scan of vllm-openai-cpu v0.32.0 @827ef381 (Trivy 0.75.0, HIGH/CRITICAL, fixed
only): libbrotli1 (apt, Dockerfile), venv certifi (uv pip install of this
project's locked export, Dockerfile), setuptools' vendored jaraco.context and
wheel, and pip's vendored certifi/urllib3/msgpack/pkg_resources. vLLM 0.32.0
pins setuptools==78.1.1, so setuptools stays and only its two vendored
packages are replaced from the setuptools wheel locked in the
"vendored-source" group of uv.lock (URL and sha256 from the lock). pip is not a
runtime dependency of anything in the image and is removed everywhere.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import urllib.request
import zipfile

SITE = Path(sys.prefix) / "lib/python3.12/site-packages"
VENDORED = {  # new member prefix in the wheel -> old paths to remove under setuptools/_vendor
    "setuptools/_vendor/jaraco/context/": ["jaraco/context.py", "jaraco/context"],
    "setuptools/_vendor/jaraco_context-6.1.0.dist-info/": ["jaraco.context-5.3.0.dist-info", "jaraco_context-5.3.0.dist-info"],
    "setuptools/_vendor/wheel/": ["wheel"],
    "setuptools/_vendor/wheel-0.46.3.dist-info/": ["wheel-0.46.3.dist-info", "wheel-0.45.1.dist-info"],
}
EXPECTED = {"certifi": "2026.10.18", "setuptools": "78.1.1"}


def locked_wheel(lock_path: str, name: str) -> tuple[str, str]:
    lock = tomllib.loads(Path(lock_path).read_text())
    package = next(p for p in lock["package"] if p["name"] == name)
    wheel = next(w for w in package["wheels"] if w["url"].endswith("-py3-none-any.whl"))
    return wheel["url"], wheel["hash"].removeprefix("sha256:")


def replace_vendored(lock_path: str) -> None:
    url, sha256 = locked_wheel(lock_path, "setuptools")
    data = urllib.request.urlopen(url, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != sha256:
        sys.exit("setuptools wheel sha256 does not match uv.lock")
    wheel = zipfile.ZipFile(io.BytesIO(data))
    vendor = SITE / "setuptools/_vendor"
    for prefix, olds in VENDORED.items():
        for old in olds:
            target = vendor / old
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        members = [n for n in wheel.namelist() if n.startswith(prefix)]
        if not members:
            sys.exit(f"{prefix} missing from the setuptools wheel")
        for name in members:
            out = SITE / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(wheel.read(name))


def remove_pip() -> None:
    """pip (and its vendored certifi/urllib3/msgpack/pkg_resources) is not used at
    runtime; the scan found it in the venv, the uv-managed base interpreter, its
    ensurepip wheel and uv's download cache."""
    base = Path(sys.base_prefix)
    base_site = base / "lib/python3.12/site-packages"
    paths = [*SITE.glob("pip"), *SITE.glob("pip-*.dist-info"), *(Path(sys.prefix) / "bin").glob("pip*"),
             *base_site.glob("pip"), *base_site.glob("pip-*.dist-info"), *(base / "bin").glob("pip*"),
             *(base / "lib/python3.12/ensurepip/_bundled").glob("*.whl"), Path("/root/.cache/uv")]
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def exercise_setuptools() -> None:
    """Run the code paths of setuptools 78.1.1 that use the swapped packages."""
    import setuptools  # noqa: F401 (puts setuptools/_vendor on sys.path)
    import jaraco.context
    import jaraco.text
    import wheel
    from setuptools import build_meta

    if wheel.__version__ != "0.46.3" or not hasattr(jaraco.context, "ExceptionTrap"):
        sys.exit("vendored wheel/jaraco.context are not the replaced versions")
    if list(jaraco.text.yield_lines("a\n\n# c\nb")) != ["a", "b"]:
        sys.exit("jaraco.text misbehaves with the replaced jaraco.context")
    with tempfile.TemporaryDirectory() as folder:  # bdist_wheel -> vendored wheel.wheelfile
        project = Path(folder)
        (project / "probe").mkdir()
        (project / "probe/__init__.py").write_text("")
        (project / "pyproject.toml").write_text('[project]\nname = "probe"\nversion = "1.0"\n'
                                                '[tool.setuptools]\npackages = ["probe"]\n')
        cwd = Path.cwd()
        os.chdir(project)
        try:
            built = build_meta.build_wheel(str(project / "dist"))
        finally:
            os.chdir(cwd)
        names = zipfile.ZipFile(project / "dist" / built).namelist()
        if "probe-1.0.dist-info/METADATA" not in names:
            sys.exit("bdist_wheel produced an incomplete wheel")


def check() -> None:
    from importlib.metadata import version

    importlib.invalidate_caches()
    for name, expected in EXPECTED.items():
        if version(name) != expected:
            sys.exit(f"{name} {version(name)} != {expected}")
    for module in ("certifi", "setuptools", "setuptools.command.bdist_wheel", "pkg_resources",
                   "torch.utils.cpp_extension", "vllm"):
        importlib.import_module(module)
    import certifi
    assert Path(certifi.where()).is_file()
    exercise_setuptools()
    if importlib.util.find_spec("pip") is not None:
        sys.exit("pip still importable")


if __name__ == "__main__":
    replace_vendored(sys.argv[1])
    remove_pip()
    check()
    print("remediation applied")
