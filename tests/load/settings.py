"""Validated load-test settings and the guard against unintended targets.

Every value has a safe default, and a run cannot start against a non-loopback
host unless the integration profile is selected and explicitly acknowledged.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from typing import Mapping
from urllib.parse import urlsplit

from tests.load.scenarios import (
    DEFAULT_MIX,
    INTEGRATION_MIX,
    INTEGRATION_PROFILE,
    LOCAL_PROFILE,
    SCENARIOS,
)


REMOTE_ACKNOWLEDGEMENT = "I_OWN_THIS_TARGET"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_SEED = 20261106


class LoadConfigurationError(ValueError):
    """The requested load run is unsafe or malformed."""


def parse_mix(text: str) -> dict[str, int]:
    """Parse ``name=weight,...`` into non-negative scenario weights."""
    mix: dict[str, int] = {}
    for item in text.split(","):
        name, separator, weight = item.strip().partition("=")
        if not separator or name not in SCENARIOS or name in mix:
            raise LoadConfigurationError(f"invalid scenario entry: {item!r}")
        try:
            value = int(weight)
        except ValueError as exc:
            raise LoadConfigurationError(f"invalid weight: {item!r}") from exc
        if value < 0:
            raise LoadConfigurationError(f"negative weight: {item!r}")
        mix[name] = value
    if not any(mix.values()):
        raise LoadConfigurationError("scenario mix has no positive weight")
    return mix


def validate_target(host: str, profile: str, acknowledgement: str | None) -> None:
    """Refuse any target other than loopback unless explicitly acknowledged."""
    parsed = urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LoadConfigurationError(f"invalid host: {host!r}")
    if parsed.hostname in LOOPBACK_HOSTS:
        return
    if profile != INTEGRATION_PROFILE:
        raise LoadConfigurationError(
            "non-loopback targets require the integration profile"
        )
    if acknowledgement != REMOTE_ACKNOWLEDGEMENT:
        raise LoadConfigurationError(
            f"set EWALLET_LOAD_ACKNOWLEDGE_REMOTE={REMOTE_ACKNOWLEDGEMENT} "
            "to confirm ownership of this target"
        )


@dataclass(frozen=True)
class LoadSettings:
    """Settings Locust reads once at start-up."""

    profile: str
    mix: Mapping[str, int]
    seed: int

    @classmethod
    def from_env(
        cls, host: str, environment: Mapping[str, str] | None = None
    ) -> "LoadSettings":
        """Validate the environment against ``host`` and build settings."""
        values = os.environ if environment is None else environment
        profile = values.get("EWALLET_LOAD_PROFILE", LOCAL_PROFILE)
        if profile not in {LOCAL_PROFILE, INTEGRATION_PROFILE}:
            raise LoadConfigurationError(f"unknown profile: {profile!r}")
        validate_target(host, profile, values.get("EWALLET_LOAD_ACKNOWLEDGE_REMOTE"))

        default_mix = DEFAULT_MIX if profile == LOCAL_PROFILE else INTEGRATION_MIX
        mix = parse_mix(values.get("EWALLET_LOAD_MIX", default_mix))
        if profile == INTEGRATION_PROFILE and mix.get("dependency_failure", 0):
            raise LoadConfigurationError(
                "dependency failures only exist in the mock-backed local profile"
            )
        try:
            seed = int(values.get("EWALLET_LOAD_SEED", str(DEFAULT_SEED)))
        except ValueError as exc:
            raise LoadConfigurationError("EWALLET_LOAD_SEED must be an integer") from exc
        return cls(profile=profile, mix=mix, seed=seed)


def main() -> None:
    """Preflight used by the runner: exit 2 unless ``argv[1]`` is an allowed host."""
    try:
        settings = LoadSettings.from_env(sys.argv[1])
    except LoadConfigurationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"target allowed: profile={settings.profile} mix={dict(settings.mix)}")


if __name__ == "__main__":
    main()
