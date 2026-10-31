"""Fail-fast environment configuration for the Block B service."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from ai_platform.inference.errors import ConfigurationError


def configure_feast_redis_timeouts(
    store: object, *, timeout_seconds: float
) -> None:
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        raise ConfigurationError()
    try:
        online_store = store.config.online_store
        store_type = online_store.type
        connection_string = online_store.connection_string
    except (AttributeError, TypeError) as exc:
        raise ConfigurationError() from exc
    if store_type != "redis" or not isinstance(connection_string, str):
        raise ConfigurationError()

    segments = connection_string.split(",")
    parameters: dict[str, tuple[int, object]] = {}
    for index, segment in enumerate(segments):
        if "=" not in segment:
            continue
        name, raw_value = segment.split("=", 1)
        if name in parameters:
            raise ConfigurationError()
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        parameters[name] = (index, value)

    for name in ("socket_connect_timeout", "socket_timeout"):
        if name not in parameters:
            segments.append(f"{name}={timeout_seconds:g}")
            continue
        _, value = parameters[name]
        if isinstance(value, bool):
            raise ConfigurationError()
        try:
            configured = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError() from exc
        if (
            not math.isfinite(configured)
            or configured <= 0
            or configured > timeout_seconds
        ):
            raise ConfigurationError()

    try:
        online_store.connection_string = ",".join(segments)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigurationError() from exc


def _timeout_setting(
    values: Mapping[str, str], name: str, default: str
) -> float:
    try:
        timeout = float(values.get(name, default))
    except ValueError as exc:
        raise ConfigurationError() from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ConfigurationError()
    return timeout


@dataclass(frozen=True)
class InferenceSettings:
    feast_repo_path: Path
    feast_timeout_seconds: float
    kserve_base_url: str
    model_name: str
    model_version: str
    kserve_timeout_seconds: float

    @classmethod
    def from_env(
        cls, environment: Mapping[str, str] | None = None
    ) -> "InferenceSettings":
        values = os.environ if environment is None else environment
        required_names = (
            "EWALLET_FEAST_REPO_PATH",
            "EWALLET_KSERVE_BASE_URL",
            "EWALLET_MODEL_NAME",
            "EWALLET_MODEL_VERSION",
        )
        if any(not values.get(name, "").strip() for name in required_names):
            raise ConfigurationError()

        feast_repo = Path(values["EWALLET_FEAST_REPO_PATH"]).resolve()
        if not feast_repo.is_dir() or not (
            feast_repo / "feature_store.yaml"
        ).is_file():
            raise ConfigurationError()

        url = values["EWALLET_KSERVE_BASE_URL"].strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError()
        feast_timeout = _timeout_setting(
            values, "EWALLET_FEAST_TIMEOUT_SECONDS", "2.0"
        )
        kserve_timeout = _timeout_setting(
            values, "EWALLET_KSERVE_TIMEOUT_SECONDS", "2.0"
        )

        model_name = values["EWALLET_MODEL_NAME"].strip()
        model_version = values["EWALLET_MODEL_VERSION"].strip()
        if len(model_name) > 128 or len(model_version) > 128:
            raise ConfigurationError()
        return cls(
            feast_repo_path=feast_repo,
            feast_timeout_seconds=feast_timeout,
            kserve_base_url=url,
            model_name=model_name,
            model_version=model_version,
            kserve_timeout_seconds=kserve_timeout,
        )
