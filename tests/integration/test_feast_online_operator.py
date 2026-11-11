"""Feast Online operator against a real Redis (set EWALLET_TEST_REDIS_URL)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from ai_platform.features.feast import online_operator
from ai_platform.features.feast.schema import FEATURE_SPECS


REDIS_URL = os.environ.get("EWALLET_TEST_REDIS_URL")
ENTITY_VALUES = {
    "user_behavior": ("u1", "u2", "u3"),
    "account_behavior": ("a1", "a2", "a3"),
    "device_behavior": ("d1", "d2", "d3"),
    "merchant_behavior": ("m1", "m2", "m3"),
}
T0 = pd.Timestamp("2026-05-31 22:00:00", tz="UTC")


def _write_offline(root: Path) -> None:
    for view_name, spec in FEATURE_SPECS.items():
        rows = []
        for entity_index, entity in enumerate(ENTITY_VALUES[view_name]):
            # three updates per entity; the last lands in the incremental window
            for step, minutes in enumerate((5, 50, 115)):
                row = {
                    spec["entity_key"]: entity,
                    "event_timestamp": T0 + pd.Timedelta(minutes=minutes + entity_index),
                }
                for feature_index, feature in enumerate(spec["features"]):
                    value = entity_index * 1000 + step * 100 + feature_index
                    row[feature] = (
                        value if feature in spec["integer_features"] else value / 7
                    )
                rows.append(row)
        frame = pd.DataFrame(rows)
        frame["event_timestamp"] = frame["event_timestamp"].astype("datetime64[us, UTC]")
        target = root / spec["table"]
        target.mkdir(parents=True)
        frame.to_parquet(target / "part-0.parquet", index=False)


class RedisTargetParsingTest(unittest.TestCase):
    def test_invalid_port_and_database_are_refused_before_connecting(self) -> None:
        for connection in ("redis:0,db=1", "redis:6379,db=-1"):
            with self.subTest(connection=connection), self.assertRaises(RuntimeError):
                online_operator._redis_connection_parts(connection)

    def test_production_store_cannot_be_claimed_for_verification(self) -> None:
        environment = {
            **self.verification_environment(),
            "EWALLET_EXECUTION_POLICY": "production",
            "EWALLET_ONLINE_STORE_ID": "canonical-online",
            "EWALLET_REDIS_SECRET_NAME": "feast-online-redis",
        }
        client = Mock()
        client.info.return_value = {"run_id": "server-1"}
        with patch.dict(os.environ, environment, clear=True), patch.object(
            online_operator, "_redis_client", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "approved verification binding"):
                online_operator.claim_online_store(
                    "redis.feature-store.svc.cluster.local:6379,db=0"
                )

    def verification_environment(self) -> dict[str, str]:
        return {
            "EWALLET_EXECUTION_POLICY": "verification",
            "EWALLET_ONLINE_STORE_ID": "canonical-verify",
            "EWALLET_REDIS_SECRET_NAME": "feast-online-redis-verify",
            "EWALLET_REDIS_CAMPAIGN_ID": "online-1",
            "EWALLET_DATASET_VERSION": "canonical-100gb-v1",
            "EWALLET_FEAST_REGISTRY": "gs://bucket/features/registries/canonical-100gb-v1/online/online-1/registry.db",
            "EWALLET_FEATURE_RUN_ID": "feature-run-1",
            "EWALLET_SNAPSHOT_ID": "snapshot-1",
            "EWALLET_ESTIMATED_REDIS_BYTES": "100",
        }

    def test_missing_execution_policy_fails_closed(self) -> None:
        environment = self.verification_environment()
        environment.pop("EWALLET_EXECUTION_POLICY")
        client = Mock()
        client.info.return_value = {"run_id": "server-1"}
        with patch.dict(os.environ, environment, clear=True), patch.object(
            online_operator, "_redis_client", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "execution policy"):
                online_operator._expected_ownership(
                    "redis.feature-store.svc.cluster.local:6379,db=1"
                )

    def test_verification_policy_cannot_bypass_checks_with_production_name(self) -> None:
        environment = {
            **self.verification_environment(),
            "EWALLET_ONLINE_STORE_ID": "canonical-online",
            "EWALLET_REDIS_SECRET_NAME": "feast-online-redis",
            "EWALLET_DATASET_VERSION": "canonical-100gb-v1",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "verification.*production|production.*verification"):
                online_operator.require_online_store_ownership(
                    "redis.feature-store.svc.cluster.local:6379,db=0"
                )

    def test_production_policy_validates_physical_target_and_preserves_authorized_use(self) -> None:
        environment = {
            **self.verification_environment(),
            "EWALLET_EXECUTION_POLICY": "production",
            "EWALLET_ONLINE_STORE_ID": "canonical-online",
            "EWALLET_REDIS_SECRET_NAME": "feast-online-redis",
        }
        client = Mock()
        client.info.return_value = {"run_id": "server-1"}
        with patch.dict(os.environ, environment, clear=True), patch.object(
            online_operator, "_redis_client", return_value=client
        ):
            online_operator.require_online_store_ownership(
                "redis.feature-store.svc.cluster.local:6379,db=0"
            )
            with self.assertRaisesRegex(RuntimeError, "approved binding"):
                online_operator.require_online_store_ownership(
                    "redis.feature-store.svc.cluster.local:6379,db=1"
                )

    def test_direct_operator_refuses_a_wrong_physical_verification_target(self) -> None:
        client = Mock()
        client.info.return_value = {"run_id": "server-1"}
        with patch.dict(os.environ, self.verification_environment()), patch.object(
            online_operator, "_redis_client", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "approved binding"):
                online_operator._expected_ownership(
                    "redis.feature-store.svc.cluster.local:6379,db=0"
                )

    def test_direct_operator_enforces_current_capacity(self) -> None:
        evidence = {
            "used_memory_bytes": 950,
            "maxmemory_bytes": 1000,
            "maxmemory_policy": "noeviction",
        }
        with patch.dict(os.environ, self.verification_environment()), patch.object(
            online_operator, "online_store_evidence", return_value=evidence
        ), patch.object(online_operator, "_redis_client") as client:
            client.return_value.info.return_value = {"run_id": "server-1"}
            with self.assertRaisesRegex(RuntimeError, "headroom"):
                online_operator.require_online_store_capacity(
                    "redis.feature-store.svc.cluster.local:6379,db=1"
                )

    def test_cloud_registry_mirror_preserves_canonical_redis_ownership(self) -> None:
        environment = {
            **self.verification_environment(),
            "EWALLET_REDIS_CONNECTION_STRING": (
                "redis.feature-store.svc.cluster.local:6379,db=1"
            ),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": "gs://bucket/features/offline",
        }
        client = Mock()
        client.eval.return_value = "CLAIMED"

        def mirror_registry():
            os.environ["EWALLET_FEAST_REGISTRY"] = "/tmp/feast-registry/registry.db"
            return Mock(generation=0)

        authorized = (
            "verification",
            client,
            {"requires_campaign_ownership": True},
            "sha256:target",
        )
        with patch.dict(os.environ, environment, clear=True), patch.object(
            online_operator, "mirror_from_environment", side_effect=mirror_registry
        ), patch.object(
            online_operator, "_authorized_target", return_value=authorized
        ), patch.object(
            online_operator, "online_store_evidence", return_value={}
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, online_operator.main(["claim"]))

        owner = json.loads(client.eval.call_args.args[3])
        self.assertEqual(
            "gs://bucket/features/registries/canonical-100gb-v1/online/online-1/registry.db",
            owner["registry_uri"],
        )


@unittest.skipUnless(REDIS_URL, "requires a disposable Redis (EWALLET_TEST_REDIS_URL)")
class FeastOnlineOperatorTest(unittest.TestCase):
    def run_command(self, *argv: str) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = online_operator.main(list(argv))
        evidence = json.loads(output.getvalue().strip().splitlines()[-1])
        evidence["exit_code"] = code
        return evidence

    def test_apply_materialize_incremental_and_parity(self) -> None:
        import redis

        redis.Redis.from_url(REDIS_URL).flushdb()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_offline(root / "offline")
            host_port = REDIS_URL.removeprefix("redis://")
            environment = {
                "EWALLET_FEAST_REGISTRY": str(root / "registry" / "registry.db"),
                "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(root / "offline"),
                "EWALLET_REDIS_CONNECTION_STRING": host_port,
                "EWALLET_EXECUTION_POLICY": "verification",
                "EWALLET_ONLINE_STORE_ID": "canonical-verify",
                "EWALLET_REDIS_SECRET_NAME": "feast-online-redis-verify",
                "EWALLET_REDIS_CAMPAIGN_ID": "local-integration",
                "EWALLET_DATASET_VERSION": "canonical-100gb-v1",
                "EWALLET_FEATURE_RUN_ID": "local-feature-run",
                "EWALLET_SNAPSHOT_ID": "local-snapshot",
                "EWALLET_ESTIMATED_REDIS_BYTES": "1000000",
            }
            client = online_operator._redis_client(host_port)
            server_run_id = str(client.info("server")["run_id"])
            fingerprint = online_operator.target_fingerprint(
                "127.0.0.1", int(host_port.rsplit(":", 1)[1]), 0, server_run_id
            )
            authorized = (
                "verification",
                client,
                {"requires_campaign_ownership": True},
                fingerprint,
            )
            with patch.dict(os.environ, environment), patch.object(
                online_operator, "_authorized_target", return_value=authorized
            ):
                (root / "registry").mkdir()
                claim = self.run_command("claim")
                self.assertEqual("CLAIMED", claim["claim_status"])
                applied = self.run_command("apply")
                self.assertEqual(sorted(FEATURE_SPECS), applied["feature_views"])
                with self.assertRaises(FileExistsError):
                    self.run_command("apply")

                window = self.run_command(
                    "materialize",
                    "--start", "2026-05-31T22:00:00+00:00",
                    "--end", "2026-05-31T23:00:00+00:00",
                )
                self.assertEqual(4, len(window["views"]))
                incremental = self.run_command(
                    "materialize-incremental", "--end", "2026-06-01T00:00:00+00:00"
                )
                for view in incremental["views"]:
                    self.assertEqual("2026-05-31T23:00:00+00:00", view["start"])

                parity = self.run_command(
                    "parity",
                    "--cutover", "2026-06-01T00:00:00+00:00",
                    "--sample-window-start", "2026-05-31T23:50:00+00:00",
                    "--sample-size", "3",
                )
                expected = 3 * sum(len(s["features"]) for s in FEATURE_SPECS.values())
                self.assertEqual("PASS", parity["status"])
                self.assertEqual(0, parity["exit_code"])
                self.assertEqual(expected, parity["comparisons"])
                self.assertEqual(31, parity["historical_features"])

                # Repeating the same window is idempotent for the stored values.
                self.run_command(
                    "materialize",
                    "--start", "2026-05-31T23:00:00+00:00",
                    "--end", "2026-06-01T00:00:00+00:00",
                )
                again = self.run_command(
                    "parity",
                    "--cutover", "2026-06-01T00:00:00+00:00",
                    "--sample-window-start", "2026-05-31T23:50:00+00:00",
                    "--sample-size", "3",
                )
                self.assertEqual("PASS", again["status"])


if __name__ == "__main__":
    unittest.main()
