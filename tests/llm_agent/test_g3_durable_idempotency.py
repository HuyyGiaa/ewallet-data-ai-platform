from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from ai_platform.llm_agent.idempotency import (
    ClaimStatus,
    IdempotencyBackendError,
    IdempotencyConflict,
    IdempotencyScope,
    IdempotencyStateError,
    InMemoryIdempotencyStore,
)
from ai_platform.llm_agent.redis_idempotency import (
    RedisIdempotencyStore,
    idempotency_store_from_env,
)


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        return self.now

    def advance(self, milliseconds: int) -> None:
        self.now += milliseconds


class DurableIdempotencyStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.store = InMemoryIdempotencyStore(clock_ms=self.clock)
        self.scope = IdempotencyScope("ewallet-agents", "kagent-controller")

    def claim(self, owner: str = "owner-1", *, fingerprint: str = "f" * 64):
        return self.store.claim(
            self.scope,
            "business-001",
            fingerprint,
            owner_token=owner,
            lease_ms=5_000,
        )

    def test_scope_is_stable_and_rejects_invalid_identity(self):
        self.assertNotEqual(
            self.scope.key("business-001"),
            IdempotencyScope("tenant-b", "kagent-controller").key("business-001"),
        )
        self.assertNotEqual(
            self.scope.key("business-001"),
            IdempotencyScope("ewallet-agents", "other-principal").key("business-001"),
        )
        self.assertNotIn("business-001", self.scope.key("business-001"))
        for tenant, principal in (("", "p"), ("t", ""), ("../x", "p")):
            with self.subTest(tenant=tenant, principal=principal), self.assertRaises(ValueError):
                IdempotencyScope(tenant, principal)

    def test_one_atomic_owner_for_concurrent_identical_claims(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(lambda i: self.claim(f"owner-{i}"), range(8)))
        self.assertEqual(sum(item.status is ClaimStatus.ACQUIRED for item in decisions), 1)
        self.assertEqual(sum(item.status is ClaimStatus.IN_PROGRESS for item in decisions), 7)
        self.assertEqual({item.generation for item in decisions}, {1})

    def test_completed_result_replays_until_explicit_ttl(self):
        claim = self.claim()
        self.store.mark_started(self.scope, "business-001", "f" * 64, claim, lease_ms=5_000)
        self.store.complete(
            self.scope,
            "business-001",
            "f" * 64,
            claim,
            result_json='{"status":"ok"}',
            completed_ttl_ms=10_000,
        )
        replay = self.claim("owner-2")
        self.assertEqual((replay.status, replay.result_json), (ClaimStatus.COMPLETED, '{"status":"ok"}'))
        self.clock.advance(10_001)
        after_ttl = self.claim("owner-3")
        self.assertEqual((after_ttl.status, after_ttl.generation), (ClaimStatus.ACQUIRED, 1))

    def test_same_identity_with_different_payload_is_conflict(self):
        self.claim()
        with self.assertRaises(IdempotencyConflict):
            self.claim("owner-2", fingerprint="e" * 64)

    def test_stale_unstarted_claim_can_be_recovered_with_new_generation(self):
        first = self.claim()
        self.clock.advance(5_001)
        second = self.claim("owner-2")
        self.assertEqual((first.generation, second.status, second.generation), (1, ClaimStatus.ACQUIRED, 2))
        with self.assertRaises(IdempotencyStateError):
            self.store.mark_started(self.scope, "business-001", "f" * 64, first, lease_ms=5_000)

    def test_stale_started_claim_becomes_persistent_ambiguous(self):
        first = self.claim()
        self.store.mark_started(self.scope, "business-001", "f" * 64, first, lease_ms=5_000)
        self.clock.advance(5_001)
        ambiguous = self.claim("owner-2")
        self.assertEqual(ambiguous.status, ClaimStatus.AMBIGUOUS)
        self.clock.advance(365 * 24 * 60 * 60 * 1_000)
        self.assertEqual(self.claim("owner-3").status, ClaimStatus.AMBIGUOUS)
        with self.assertRaises(IdempotencyStateError):
            self.store.complete(
                self.scope,
                "business-001",
                "f" * 64,
                first,
                result_json='{"status":"late"}',
                completed_ttl_ms=10_000,
            )

    def test_release_is_allowed_only_before_downstream_started(self):
        claim = self.claim()
        self.store.release_unstarted(self.scope, "business-001", "f" * 64, claim)
        self.assertEqual(self.claim("owner-2").status, ClaimStatus.ACQUIRED)

        other = self.store.claim(
            self.scope, "business-002", "a" * 64, owner_token="owner-a", lease_ms=5_000,
        )
        self.store.mark_started(self.scope, "business-002", "a" * 64, other, lease_ms=5_000)
        with self.assertRaises(IdempotencyStateError):
            self.store.release_unstarted(self.scope, "business-002", "a" * 64, other)

    def test_result_size_is_bounded(self):
        claim = self.claim()
        self.store.mark_started(self.scope, "business-001", "f" * 64, claim, lease_ms=5_000)
        with self.assertRaises(ValueError):
            self.store.complete(
                self.scope,
                "business-001",
                "f" * 64,
                claim,
                result_json="x" * 32_769,
                completed_ttl_ms=10_000,
            )


class FakeRedis:
    def __init__(self, replies=None, failure=None) -> None:
        self.replies = list(replies or [])
        self.failure = failure
        self.calls = []

    def eval(self, script, key_count, *arguments):
        self.calls.append((script, key_count, arguments))
        if self.failure:
            raise self.failure
        return self.replies.pop(0)

    def ping(self):
        if self.failure:
            raise self.failure
        return True


class RedisIdempotencyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = IdempotencyScope("ewallet-agents", "kagent-controller")

    def test_atomic_claim_decodes_only_the_bounded_protocol(self):
        client = FakeRedis([[b"acquired", b"7", b""]])
        store = RedisIdempotencyStore(client)
        claim = store.claim(
            self.scope, "request-1", "f" * 64, owner_token="owner-1", lease_ms=5_000,
        )
        self.assertEqual((claim.status, claim.generation), (ClaimStatus.ACQUIRED, 7))
        self.assertEqual(client.calls[0][1], 1)
        self.assertIn("TIME", client.calls[0][0])

    def test_redis_outage_and_malformed_reply_fail_closed(self):
        for client in (FakeRedis(failure=OSError("down")), FakeRedis([[]])):
            with self.subTest(client=client), self.assertRaises(IdempotencyBackendError) as seen:
                RedisIdempotencyStore(client).claim(
                    self.scope, "request-1", "f" * 64,
                    owner_token="owner-1", lease_ms=5_000,
                )
            self.assertIn("Redis", str(seen.exception))

    def test_readiness_requires_a_successful_redis_ping(self):
        self.assertIsNone(RedisIdempotencyStore(FakeRedis()).ready())
        with self.assertRaises(IdempotencyBackendError):
            RedisIdempotencyStore(FakeRedis(failure=OSError("down"))).ready()

    def test_production_factory_requires_authenticated_bounded_redis_configuration(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError):
            idempotency_store_from_env()
        with patch.dict(
            "os.environ",
            {"EWALLET_IDEMPOTENCY_REDIS_URL": "redis://redis.internal:6379/0"},
            clear=True,
        ), self.assertRaises(ValueError):
            idempotency_store_from_env()
        with patch.dict(
            "os.environ",
            {
                "EWALLET_IDEMPOTENCY_REDIS_URL": "redis://:secret@redis.internal:6379/0",
                "EWALLET_IDEMPOTENCY_REDIS_TIMEOUT_SECONDS": "30",
            },
            clear=True,
        ), self.assertRaises(RuntimeError):
            idempotency_store_from_env()

    def test_redis_completion_validates_generation_and_uses_explicit_ttl(self):
        client = FakeRedis([[b"ok", b"3", b""]])
        store = RedisIdempotencyStore(client)
        store.complete(
            self.scope,
            "request-1",
            "f" * 64,
            claim=type("ClaimValue", (), {
                "status": ClaimStatus.ACQUIRED,
                "owner_token": "owner-1",
                "generation": 3,
            })(),
            result_json='{"status":"ok"}',
            completed_ttl_ms=86_400_000,
        )
        arguments = client.calls[0][2]
        self.assertIn("86400000", tuple(str(item) for item in arguments))

if __name__ == "__main__":
    unittest.main()
