"""Validated immutable models for platform runtime configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping


StorageLayer = Literal["source", "bronze", "silver", "gold", "feast"]

_SUPPORTED_ENVIRONMENTS = frozenset({"local", "dev", "gcp"})
_SUPPORTED_STORAGE_SCHEMES = frozenset({"file", "s3a", "gs"})
_TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, field_name)


def _immutable_string_mapping(
    value: Mapping[str, str],
    field_name: str,
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")

    normalized: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = _non_empty_string(key, f"{field_name} key")
        if not isinstance(item, str):
            raise ValueError(f"{field_name}.{normalized_key} must be a string")
        normalized[normalized_key] = item
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class StorageConfig:
    """Physical storage roots selected by one environment profile."""

    scheme: str
    source_root: str
    bronze_root: str
    silver_root: str
    gold_root: str
    feast_root: str
    options: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        scheme = _non_empty_string(self.scheme, "storage.scheme").lower()
        if scheme not in _SUPPORTED_STORAGE_SCHEMES:
            supported = ", ".join(sorted(_SUPPORTED_STORAGE_SCHEMES))
            raise ValueError(
                f"storage.scheme must be one of: {supported}; got {scheme!r}"
            )
        object.__setattr__(self, "scheme", scheme)

        for field_name in (
            "source_root",
            "bronze_root",
            "silver_root",
            "gold_root",
            "feast_root",
        ):
            root = _non_empty_string(
                getattr(self, field_name),
                f"storage.{field_name}",
            ).rstrip("/")
            object.__setattr__(self, field_name, root)

        object.__setattr__(
            self,
            "options",
            _immutable_string_mapping(self.options, "storage.options"),
        )

    def table_uri(self, layer: StorageLayer, table_name: str) -> str:
        """Return a normalized physical URI for one logical table."""

        roots = {
            "source": self.source_root,
            "bronze": self.bronze_root,
            "silver": self.silver_root,
            "gold": self.gold_root,
            "feast": self.feast_root,
        }
        if layer not in roots:
            supported = ", ".join(roots)
            raise ValueError(f"layer must be one of: {supported}; got {layer!r}")
        if not isinstance(table_name, str) or not _TABLE_NAME_PATTERN.fullmatch(
            table_name
        ):
            raise ValueError(
                "table_name must be one non-empty relative name containing only "
                "letters, digits, dot, underscore, or hyphen"
            )
        return f"{roots[layer]}/{table_name}"


@dataclass(frozen=True)
class SparkConfig:
    """Spark settings that vary by execution environment."""

    master: str | None
    shuffle_partitions: int
    event_log_uri: str
    packages: tuple[str, ...]
    configurations: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "master",
            _optional_string(self.master, "spark.master"),
        )
        if (
            isinstance(self.shuffle_partitions, bool)
            or not isinstance(self.shuffle_partitions, int)
            or self.shuffle_partitions <= 0
        ):
            raise ValueError("spark.shuffle_partitions must be a positive integer")
        object.__setattr__(
            self,
            "event_log_uri",
            _non_empty_string(self.event_log_uri, "spark.event_log_uri").rstrip(
                "/"
            ),
        )

        if not isinstance(self.packages, (tuple, list)):
            raise ValueError("spark.packages must be a list or tuple")
        packages = tuple(
            _non_empty_string(package, "spark.packages item")
            for package in self.packages
        )
        object.__setattr__(self, "packages", packages)
        object.__setattr__(
            self,
            "configurations",
            _immutable_string_mapping(
                self.configurations,
                "spark.configurations",
            ),
        )


@dataclass(frozen=True)
class ServiceConfig:
    """Endpoints for optional platform services."""

    datahub_gms_url: str
    trino_host: str | None
    trino_port: int | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "datahub_gms_url",
            _non_empty_string(
                self.datahub_gms_url,
                "services.datahub_gms_url",
            ).rstrip("/"),
        )
        object.__setattr__(
            self,
            "trino_host",
            _optional_string(self.trino_host, "services.trino_host"),
        )
        if self.trino_port is not None and (
            isinstance(self.trino_port, bool)
            or not isinstance(self.trino_port, int)
            or not 1 <= self.trino_port <= 65535
        ):
            raise ValueError("services.trino_port must be between 1 and 65535")
        if (self.trino_host is None) != (self.trino_port is None):
            raise ValueError(
                "services.trino_host and services.trino_port must both be set "
                "or both be null"
            )


@dataclass(frozen=True)
class RuntimeConfig:
    """Complete runtime configuration for one named environment."""

    environment: str
    project_id: str | None
    region: str | None
    storage: StorageConfig
    spark: SparkConfig
    services: ServiceConfig

    def __post_init__(self) -> None:
        environment = _non_empty_string(self.environment, "environment").lower()
        if environment not in _SUPPORTED_ENVIRONMENTS:
            supported = ", ".join(sorted(_SUPPORTED_ENVIRONMENTS))
            raise ValueError(
                f"environment must be one of: {supported}; got {environment!r}"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "project_id",
            _optional_string(self.project_id, "project_id"),
        )
        object.__setattr__(
            self,
            "region",
            _optional_string(self.region, "region"),
        )

        if not isinstance(self.storage, StorageConfig):
            raise ValueError("storage must be a StorageConfig")
        if not isinstance(self.spark, SparkConfig):
            raise ValueError("spark must be a SparkConfig")
        if not isinstance(self.services, ServiceConfig):
            raise ValueError("services must be a ServiceConfig")
