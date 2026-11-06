"""Fail-closed boundaries of environment and Redis timeout configuration."""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ai_platform.inference.config import (
    InferenceSettings,
    configure_feast_redis_timeouts,
)
from ai_platform.inference.errors import ConfigurationError


def store_with(connection_string: object, store_type: str = "redis"):
    """Build a store exposing only the online-store attributes used."""
    online_store = SimpleNamespace(
        type=store_type, connection_string=connection_string
    )
    return SimpleNamespace(config=SimpleNamespace(online_store=online_store))


class FrozenOnlineStore:
    """Online store rejecting writes, as an immutable config object would."""

    type = "redis"
    connection_string = "redis.invalid:6379"

    def __setattr__(self, name, value):
        raise TypeError("frozen")


class RedisTimeoutConfigurationTest(unittest.TestCase):
    def test_timeout_argument_must_be_finite_within_the_bound(self):
        store = store_with("redis.invalid:6379")
        for timeout in (0, -1, 60.5, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ConfigurationError):
                    configure_feast_redis_timeouts(store, timeout_seconds=timeout)

    def test_upper_bound_is_inclusive(self):
        store = store_with("redis.invalid:6379")
        configure_feast_redis_timeouts(store, timeout_seconds=60)
        self.assertIn(
            "socket_timeout=60", store.config.online_store.connection_string
        )

    def test_non_string_connection_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            configure_feast_redis_timeouts(store_with(None), timeout_seconds=1)

    def test_tighter_existing_deadlines_are_preserved(self):
        original = (
            "redis.invalid:6379,socket_connect_timeout=0.5,socket_timeout=1"
        )
        store = store_with(original)
        configure_feast_redis_timeouts(store, timeout_seconds=2)
        self.assertEqual(original, store.config.online_store.connection_string)

    def test_only_the_missing_deadline_is_appended(self):
        store = store_with("redis.invalid:6379,socket_timeout=1")
        configure_feast_redis_timeouts(store, timeout_seconds=2)
        self.assertEqual(
            "redis.invalid:6379,socket_timeout=1,socket_connect_timeout=2",
            store.config.online_store.connection_string,
        )

    def test_invalid_existing_deadlines_fail_closed(self):
        for segment in (
            "socket_timeout=true",
            "socket_timeout=abc",
            "socket_timeout=0",
            "socket_timeout=-1",
            "socket_timeout=NaN",
            "socket_timeout=null",
            "socket_timeout=3",
        ):
            with self.subTest(segment=segment):
                with self.assertRaises(ConfigurationError):
                    configure_feast_redis_timeouts(
                        store_with(f"redis.invalid:6379,{segment}"),
                        timeout_seconds=2,
                    )

    def test_duplicate_parameter_is_ambiguous_and_rejected(self):
        with self.assertRaises(ConfigurationError):
            configure_feast_redis_timeouts(
                store_with("host:1,socket_timeout=1,socket_timeout=2"),
                timeout_seconds=2,
            )

    def test_immutable_store_configuration_fails_closed(self):
        store = SimpleNamespace(
            config=SimpleNamespace(online_store=FrozenOnlineStore())
        )
        with self.assertRaises(ConfigurationError):
            configure_feast_redis_timeouts(store, timeout_seconds=2)


class EnvironmentSettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        (self.repo / "feature_store.yaml").write_text("project: synthetic\n")

    def environment(self, **overrides):
        values = {
            "EWALLET_FEAST_REPO_PATH": str(self.repo),
            "EWALLET_KSERVE_BASE_URL": "http://kserve.invalid",
            "EWALLET_MODEL_NAME": "fraud-model",
            "EWALLET_MODEL_VERSION": "7",
        }
        values.update(overrides)
        return values

    def test_values_are_normalized(self):
        settings = InferenceSettings.from_env(
            self.environment(
                EWALLET_KSERVE_BASE_URL=" https://kserve.invalid/ ",
                EWALLET_MODEL_NAME=" fraud-model ",
                EWALLET_MODEL_VERSION=" 7 ",
            )
        )
        self.assertEqual("https://kserve.invalid", settings.kserve_base_url)
        self.assertEqual("fraud-model", settings.model_name)
        self.assertEqual("7", settings.model_version)

    def test_whitespace_required_value_is_absent(self):
        with self.assertRaises(ConfigurationError):
            InferenceSettings.from_env(self.environment(EWALLET_MODEL_NAME="  "))

    def test_feast_repo_requires_a_feature_store_file(self):
        (self.repo / "feature_store.yaml").unlink()
        with self.assertRaises(ConfigurationError):
            InferenceSettings.from_env(self.environment())

    def test_kserve_url_must_be_a_plain_http_origin(self):
        for url in (
            "ftp://kserve.invalid",
            "http://",
            "http://kserve.invalid?token=x",
            "http://kserve.invalid#fragment",
            "http://user@kserve.invalid",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigurationError):
                    InferenceSettings.from_env(
                        self.environment(EWALLET_KSERVE_BASE_URL=url)
                    )

    def test_timeouts_must_be_numeric_and_within_the_bound(self):
        for name in (
            "EWALLET_FEAST_TIMEOUT_SECONDS",
            "EWALLET_KSERVE_TIMEOUT_SECONDS",
        ):
            for value in ("abc", "0", "-1", "60.1"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigurationError):
                        InferenceSettings.from_env(
                            self.environment(**{name: value})
                        )

    def test_model_identity_length_boundary(self):
        accepted = InferenceSettings.from_env(
            self.environment(
                EWALLET_MODEL_NAME="m" * 128, EWALLET_MODEL_VERSION="v" * 128
            )
        )
        self.assertEqual(128, len(accepted.model_name))
        for name in ("EWALLET_MODEL_NAME", "EWALLET_MODEL_VERSION"):
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError):
                    InferenceSettings.from_env(self.environment(**{name: "x" * 129}))


if __name__ == "__main__":
    unittest.main()
