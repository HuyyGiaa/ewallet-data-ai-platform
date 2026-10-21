"""Validate runtime profiles and reject stale physical runtime literals."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from data_platform.config import load_runtime_config


SCAN_ROOTS = (
    "data_platform",
    "ai_platform",
    "configs/environments",
    "infra/docker",
)
SOURCE_SUFFIXES = frozenset({".py", ".yaml", ".yml"})
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".venv", "__pycache__", "node_modules"}
)

_FORBIDDEN_LITERALS = (
    ("s3a://bronze-zone", re.compile(r"s3a://bronze-zone")),
    ("s3a://silver-zone", re.compile(r"s3a://silver-zone")),
    ("s3a://gold-zone", re.compile(r"s3a://gold-zone")),
    ("s3://gold-zone", re.compile(r"s3://gold-zone")),
    ("localhost:8080", re.compile(r"(?:https?://)?localhost:8080")),
    ("localhost:8081", re.compile(r"(?:https?://)?localhost:8081")),
    ("minioadmin", re.compile(r"\bminioadmin\b")),
    ("developer absolute path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")),
)

# Exceptions are individual local-runtime files and individual literal types.
# Adding an entire source directory here would defeat the contract.
_ALLOWLIST = {
    "configs/environments/local/platform.yaml": {
        "s3a://bronze-zone",
        "s3a://silver-zone",
        "s3a://gold-zone",
        "localhost:8080",
    },
    "infra/docker/compose.storage.yml": {"minioadmin"},
    "data_platform/metadata/datahub/lineage/trino_recipe.yml": {
        "localhost:8080",
        "localhost:8081",
    },
}

_LOCAL_PROFILE_ENVIRONMENT = {
    "MINIO_ACCESS_KEY": "validation-access-key",
    "MINIO_SECRET_KEY": "validation-secret-key",
}
_CLOUD_PROFILE_ENVIRONMENT = {
    "GCP_PROJECT_ID": "validation-project",
    "GCP_REGION": "asia-southeast1",
    "EWALLET_DATA_BUCKET": "validation-data-bucket",
    "DATAHUB_GMS_URL": "http://datahub-gms.metadata.svc.cluster.local:8080",
}


@dataclass(frozen=True)
class LiteralViolation:
    path: str
    line: int
    literal: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: forbidden runtime literal {self.literal!r}"


@dataclass(frozen=True)
class ValidationResult:
    check: str
    status: str
    detail: str


def _source_paths(repository_root: Path):
    for relative_root in SCAN_ROOTS:
        scan_root = repository_root / relative_root
        if not scan_root.exists():
            continue
        for path in sorted(scan_root.rglob("*")):
            relative_parts = path.relative_to(repository_root).parts
            if EXCLUDED_DIRECTORY_NAMES.intersection(relative_parts):
                continue
            if any(part.startswith(".") for part in relative_parts[:-1]):
                continue
            if path.is_file() and path.suffix in SOURCE_SUFFIXES:
                yield path


def scan_runtime_literals(repository_root: Path = REPOSITORY_ROOT) -> list[LiteralViolation]:
    """Return stale physical runtime literals outside exact local exceptions."""
    root = repository_root.resolve()
    violations: list[LiteralViolation] = []
    for path in _source_paths(root):
        relative = path.relative_to(root).as_posix()
        allowed = _ALLOWLIST.get(relative, set())
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            for literal, pattern in _FORBIDDEN_LITERALS:
                if literal not in allowed and pattern.search(line):
                    violations.append(
                        LiteralViolation(relative, line_number, literal)
                    )
    return violations


def _validate_profiles() -> str:
    profiles = {
        "local": _LOCAL_PROFILE_ENVIRONMENT,
        "dev": _CLOUD_PROFILE_ENVIRONMENT,
        "gcp": _CLOUD_PROFILE_ENVIRONMENT,
    }
    for environment, values in profiles.items():
        config = load_runtime_config(environment, environ=values)
        if config.environment != environment:
            raise ValueError(
                f"profile mismatch: expected {environment}, got {config.environment}"
            )
    return f"{len(profiles)} profiles parsed"


def _validate_literals() -> str:
    violations = scan_runtime_literals()
    if violations:
        raise ValueError("; ".join(map(str, violations)))
    return "0 stale runtime literals"


def _validate_contracts() -> str:
    contract_root = REPOSITORY_ROOT / "data_platform" / "contracts"
    contracts = sorted(contract_root.glob("*.yml"))
    if len(contracts) != 9:
        raise ValueError(f"expected 9 contracts, found {len(contracts)}")
    for path in contracts:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{path.name} is not a YAML mapping")
        for field in ("dataset", "storage_layer", "storage_table"):
            if not document.get(field):
                raise ValueError(f"{path.name} missing {field}")
        raw = path.read_text(encoding="utf-8").lower()
        if any(value in raw for value in ("s3a://", "s3://", "gs://", "/home/")):
            raise ValueError(f"{path.name} contains a physical location")
    return f"{len(contracts)} logical contracts parsed"


def run_validation() -> list[ValidationResult]:
    checks: tuple[tuple[str, Callable[[], str]], ...] = (
        ("profiles", _validate_profiles),
        ("runtime_literals", _validate_literals),
        ("contracts", _validate_contracts),
    )
    results = []
    for name, check in checks:
        try:
            detail = check()
        except Exception as exc:
            results.append(ValidationResult(name, "FAIL", str(exc)))
        else:
            results.append(ValidationResult(name, "PASS", detail))
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate environment runtime configuration boundaries."
    )
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = run_validation()
    for result in results:
        print(f"{result.status} | {result.check} | {result.detail}")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps([asdict(result) for result in results], indent=2) + "\n",
            encoding="utf-8",
        )
    return 0 if all(result.status == "PASS" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
