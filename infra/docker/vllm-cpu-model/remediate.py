"""Fix the Trivy HIGH/CRITICAL findings of the pinned vLLM CPU base, in place.

Scan of vllm-openai-cpu v0.32.0 @827ef381 (Trivy 0.75.0, HIGH/CRITICAL, fixed
only): libbrotli1 (apt, Dockerfile), venv certifi, setuptools' vendored
jaraco.context and wheel, and pip's vendored certifi/urllib3/msgpack/
pkg_resources. vLLM 0.32.0 pins setuptools==78.1.1, so setuptools stays and
only its two vendored packages are replaced from the setuptools 82.0.1 wheel.
pip is not a runtime dependency of anything in the image and is removed after
the hash-checked certifi install. Every download is pinned by sha256.
"""

from __future__ import annotations

import hashlib
import importlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

SITE = Path(sys.prefix) / "lib/python3.12/site-packages"
CERTIFI = "certifi==2026.10.18 --hash=sha256:73191c47aaa79da394e8fee2dbc2ec827432cab56311122c9e47abf79e390f62\n"
SETUPTOOLS_WHEEL = ("https://files.pythonhosted.org/packages/9d/76/f789f7a86709c6b087c5a2f52f911838cad707cc613162401badc665acfe/"
                    "setuptools-82.0.1-py3-none-any.whl")
SETUPTOOLS_SHA256 = "a59e362652f08dcd477c78bb6e7bd9d80a7995bc73ce773050228a348ce2e5bb"
VENDORED = {  # new member prefix in the wheel -> old paths to remove under setuptools/_vendor
    "setuptools/_vendor/jaraco/context/": ["jaraco/context.py", "jaraco/context"],
    "setuptools/_vendor/jaraco_context-6.1.0.dist-info/": ["jaraco.context-5.3.0.dist-info", "jaraco_context-5.3.0.dist-info"],
    "setuptools/_vendor/wheel/": ["wheel"],
    "setuptools/_vendor/wheel-0.46.3.dist-info/": ["wheel-0.45.1.dist-info"],
}


def install_certifi() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as req:
        req.write(CERTIFI)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--require-hashes",
                    "-r", req.name], check=True)


def replace_vendored() -> None:
    data = urllib.request.urlopen(SETUPTOOLS_WHEEL, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != SETUPTOOLS_SHA256:
        sys.exit("setuptools wheel sha256 mismatch")
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


def check() -> None:
    importlib.invalidate_caches()
    for module in ("certifi", "setuptools", "setuptools.command.bdist_wheel", "pkg_resources",
                   "torch.utils.cpp_extension", "vllm"):
        importlib.import_module(module)
    import certifi
    assert Path(certifi.where()).is_file()
    if importlib.util.find_spec("pip") is not None:
        sys.exit("pip still importable")


if __name__ == "__main__":
    install_certifi()
    replace_vendored()
    remove_pip()
    check()
    print("remediation applied")
