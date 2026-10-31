"""Load validated platform configuration from an environment profile."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from data_platform.config.models import (
    RuntimeConfig,
    ServiceConfig,
    SparkConfig,
    StorageConfig,
)


DEFAULT_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs" / "environments"
SUPPORTED_ENVIRONMENTS = frozenset({"local", "dev", "gcp"})

_PLACEHOLDER = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)

_TOP_LEVEL_FIELDS = frozenset(
    {"environment", "project_id", "region", "storage", "spark", "services"}
)
_STORAGE_FIELDS = frozenset(
    {
        "scheme",
        "source_root",
        "bronze_root",
        "silver_root",
        "gold_root",
        "feast_root",
        "options",
    }
)
_SPARK_FIELDS = frozenset(
    {
        "master",
        "shuffle_partitions",
        "event_log_uri",
        "packages",
        "configurations",
    }
)
_SERVICE_FIELDS = frozenset(
    {"datahub_gms_url", "trino_host", "trino_port"}
)


def _interpolate_string(value: str, environ: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        candidate = environ.get(name)
        if candidate:
            return candidate
        if default is not None:
            return default
        raise ValueError(f"Missing required environment variable: {name}")

    return _PLACEHOLDER.sub(replace, value)


def interpolate_environment(
    value: object,
    environ: Mapping[str, str],
) -> object:
    """Recursively render environment placeholders without mutating input."""

    if isinstance(value, str):
        return _interpolate_string(value, environ)
    if isinstance(value, list):
        return [interpolate_environment(item, environ) for item in value]
    if isinstance(value, tuple):
        return tuple(interpolate_environment(item, environ) for item in value)
    if isinstance(value, Mapping):
        return {
            key: interpolate_environment(item, environ)
            for key, item in value.items()
        }
    return value


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} keys must be strings")
    return dict(value)


def _reject_unknown(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    field_name: str,
) -> None:
    unknown = sorted(set(mapping).difference(allowed))
    if unknown:
        raise ValueError(
            f"{field_name} contains unexpected field(s): {', '.join(unknown)}"
        )


def _required(mapping: Mapping[str, Any], field_name: str) -> Any:
    if field_name not in mapping:
        raise ValueError(f"Missing required configuration field: {field_name}")
    return mapping[field_name]


def _build_runtime_config(
    raw: Mapping[str, Any],
    selected_environment: str,
) -> RuntimeConfig:
    top_level = _mapping(raw, "configuration")
    _reject_unknown(top_level, _TOP_LEVEL_FIELDS, "configuration")

    profile_environment = _required(top_level, "environment")
    if profile_environment != selected_environment:
        raise ValueError(
            "configuration.environment does not match selected environment: "
            f"{profile_environment!r} != {selected_environment!r}"
        )

    storage_values = _mapping(_required(top_level, "storage"), "storage")
    spark_values = _mapping(_required(top_level, "spark"), "spark")
    service_values = _mapping(_required(top_level, "services"), "services")
    _reject_unknown(storage_values, _STORAGE_FIELDS, "storage")
    _reject_unknown(spark_values, _SPARK_FIELDS, "spark")
    _reject_unknown(service_values, _SERVICE_FIELDS, "services")

    storage = StorageConfig(
        scheme=_required(storage_values, "scheme"),
        source_root=_required(storage_values, "source_root"),
        bronze_root=_required(storage_values, "bronze_root"),
        silver_root=_required(storage_values, "silver_root"),
        gold_root=_required(storage_values, "gold_root"),
        feast_root=_required(storage_values, "feast_root"),
        options=storage_values.get("options", {}),
    )
    spark = SparkConfig(
        master=_required(spark_values, "master"),
        shuffle_partitions=_required(spark_values, "shuffle_partitions"),
        event_log_uri=_required(spark_values, "event_log_uri"),
        packages=_required(spark_values, "packages"),
        configurations=_required(spark_values, "configurations"),
    )
    services = ServiceConfig(
        datahub_gms_url=_required(service_values, "datahub_gms_url"),
        trino_host=_required(service_values, "trino_host"),
        trino_port=_required(service_values, "trino_port"),
    )
    return RuntimeConfig(
        environment=profile_environment,
        project_id=_required(top_level, "project_id"),
        region=_required(top_level, "region"),
        storage=storage,
        spark=spark,
        services=services,
    )


def load_runtime_config(
    environment: str | None = None,
    *,
    config_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Load one named environment profile and validate its complete schema."""

    effective_environ = os.environ if environ is None else environ
    selected_environment = (
        environment or effective_environ.get("EWALLET_ENV") or "local"
    ).strip().lower()
    if selected_environment not in SUPPORTED_ENVIRONMENTS:
        supported = ", ".join(sorted(SUPPORTED_ENVIRONMENTS))
        raise ValueError(
            f"unsupported environment {selected_environment!r}; "
            f"expected one of: {supported}"
        )

    root = Path(config_root) if config_root is not None else DEFAULT_CONFIG_ROOT
    profile_path = root / selected_environment / "platform.yaml"
    if not profile_path.is_file():
        raise ValueError(
            f"Configuration profile does not exist: {profile_path}"
        )

    try:
        parsed = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML configuration: {profile_path}") from exc

    raw = _mapping(parsed, "configuration")
    rendered = interpolate_environment(raw, effective_environ)
    return _build_runtime_config(
        _mapping(rendered, "configuration"),
        selected_environment,
    )


def load_runtime_config_file(
    profile_path: Path,
    environment: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Load one explicitly distributed runtime profile file."""

    selected_environment = environment.strip().lower()
    if selected_environment not in SUPPORTED_ENVIRONMENTS:
        supported = ", ".join(sorted(SUPPORTED_ENVIRONMENTS))
        raise ValueError(
            f"unsupported environment {selected_environment!r}; "
            f"expected one of: {supported}"
        )
    path = Path(profile_path)
    if not path.is_file():
        raise ValueError(f"Configuration profile does not exist: {path}")
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML configuration: {path}") from exc
    effective_environ = os.environ if environ is None else environ
    raw = _mapping(parsed, "configuration")
    rendered = interpolate_environment(raw, effective_environ)
    return _build_runtime_config(
        _mapping(rendered, "configuration"),
        selected_environment,
    )
