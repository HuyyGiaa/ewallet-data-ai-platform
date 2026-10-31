"""Preflight command for validating one runtime configuration profile."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from data_platform.config.loader import load_runtime_config


def _redacted_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if not parsed.scheme:
        return "<redacted>"

    path = parsed.path.rstrip("/") or "/"
    if parsed.netloc:
        return f"{parsed.scheme}://<redacted>{path}"
    return f"{parsed.scheme}://<redacted>"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an E-Wallet runtime environment profile.",
    )
    parser.add_argument(
        "--environment",
        choices=("local", "dev", "gcp"),
        required=True,
        help="Runtime profile to validate.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        required=True,
        help="Run validation without starting platform services.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    effective_environ = os.environ if environ is None else environ

    try:
        config = load_runtime_config(
            args.environment,
            environ=effective_environ,
        )
    except (OSError, ValueError) as exc:
        print(f"CONFIG CHECK FAIL: {exc}", file=sys.stderr)
        return 2

    print(f"environment: {config.environment}")
    print(f"scheme: {config.storage.scheme}")
    print(f"project_id: {config.project_id or '<none>'}")
    print(f"region: {config.region or '<none>'}")
    print(f"source_root: {_redacted_uri(config.storage.source_root)}")
    print(f"bronze_root: {_redacted_uri(config.storage.bronze_root)}")
    print(f"silver_root: {_redacted_uri(config.storage.silver_root)}")
    print(f"gold_root: {_redacted_uri(config.storage.gold_root)}")
    print(f"feast_root: {_redacted_uri(config.storage.feast_root)}")
    print("CONFIG CHECK PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
