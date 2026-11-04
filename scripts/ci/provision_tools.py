"""Provision pinned, checksum-verified CLI tools for component CI.

The Jenkins controller image ships no Helm. Chart tests skip or error without it,
so CI fetches the exact release named in jenkins/ci-tools.json, verifies the
archive and the extracted binary against pinned SHA-256 digests, and prints the
directory to prepend to PATH. A cached binary is re-verified on every use; any
mismatch fails closed. Nothing is executed from the download.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
from typing import Callable
from urllib.parse import urlparse
import urllib.request

Fetch = Callable[[str], bytes]
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


class ToolError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _https_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - https enforced below
        if urlparse(response.geturl()).scheme != "https":
            raise ToolError(f"redirected away from https: {response.geturl()}")
        return response.read()


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ToolError("unsupported ci-tools schema_version")
    tools = data.get("tools") or {}
    for name, spec in tools.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ToolError(f"invalid tool name: {name}")
        if not VERSION.match(spec.get("version", "")):
            raise ToolError(f"{name}: version must be an exact vX.Y.Z release")
        url = spec.get("url", "")
        if urlparse(url).scheme != "https" or spec["version"] not in url or "latest" in url:
            raise ToolError(f"{name}: url must be an https URL naming the pinned version")
        for key in ("archive_sha256", "binary_sha256"):
            if not SHA256.match(spec.get(key, "")):
                raise ToolError(f"{name}: {key} must be a 64-character sha256")
        member = spec.get("member", "")
        if not member or member.startswith("/") or ".." in Path(member).parts:
            raise ToolError(f"{name}: unsafe archive member {member!r}")
    return tools


def provision(name: str, spec: dict[str, str], cache_dir: Path, fetch: Fetch = _https_fetch) -> Path:
    """Return a directory containing the verified ``name`` executable."""

    tool_dir = cache_dir / f"{name}-{spec['version']}-{spec['binary_sha256'][:12]}"
    binary = tool_dir / name
    if binary.is_file():
        if _sha256(binary.read_bytes()) != spec["binary_sha256"]:
            raise ToolError(f"{name}: cached binary {binary} does not match the pinned sha256")
        return tool_dir
    archive = fetch(spec["url"])
    if _sha256(archive) != spec["archive_sha256"]:
        raise ToolError(f"{name}: archive sha256 mismatch for {spec['url']}")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        try:
            member = tar.getmember(spec["member"])
        except KeyError as error:
            raise ToolError(f"{name}: archive has no member {spec['member']}") from error
        if not member.isfile():
            raise ToolError(f"{name}: archive member {spec['member']} is not a regular file")
        extracted = tar.extractfile(member)
        content = extracted.read() if extracted else b""
    if _sha256(content) != spec["binary_sha256"]:
        raise ToolError(f"{name}: extracted binary sha256 mismatch")
    cache_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=cache_dir))
    try:
        staged = staging / name
        staged.write_bytes(content)
        staged.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        os.replace(staging, tool_dir)
    except OSError:
        if not binary.is_file():
            raise
    finally:
        if staging.exists():
            for leftover in staging.iterdir():
                leftover.unlink()
            staging.rmdir()
    if _sha256(binary.read_bytes()) != spec["binary_sha256"]:
        raise ToolError(f"{name}: installed binary sha256 mismatch")
    return tool_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("jenkins/ci-tools.json"))
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, help="write the verified tool versions and digests here")
    args = parser.parse_args(argv)
    try:
        tools = load_manifest(args.manifest)
        directories = [provision(name, spec, args.cache_dir) for name, spec in sorted(tools.items())]
    except (ToolError, OSError, tarfile.TarError) as error:
        print(f"ci tool provisioning failed: {error}", file=sys.stderr)
        return 1
    if args.provenance:
        args.provenance.write_text(json.dumps({
            name: {k: spec[k] for k in ("version", "url", "archive_sha256", "binary_sha256")}
            for name, spec in sorted(tools.items())
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(os.pathsep.join(str(directory) for directory in directories))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
