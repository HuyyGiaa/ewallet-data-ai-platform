"""Window, retry and readback rules for incremental Feast materialization."""

from __future__ import annotations

import unittest
import hashlib
from datetime import datetime, timedelta, timezone

from ai_platform.features.feast.cloud_boundary import CloudFeastTrainingBoundary
from ai_platform.features.feast.materialization_plan import (
    ExistingJob,
    JobAction,
    MaterializationRequest,
    ReadbackError,
    ViewState,
    WindowError,
    WindowState,
    decide_window,
    existing_job_from_kubernetes,
    job_name,
    parse_utc,
    provenance_digest,
    resolve_materialize_job,
    verify_completion,
    verify_readback,
    verify_online_store,
)
from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.redis_isolation import RedisIsolationError, claim


UTC = timezone.utc
T22 = datetime(2026, 5, 31, 22, tzinfo=UTC)
T23 = datetime(2026, 5, 31, 23, tzinfo=UTC)
T00 = datetime(2026, 6, 1, tzinfo=UTC)
SOURCE_MAX = datetime(2026, 5, 31, 23, 59, 58, 617000, tzinfo=UTC)


def request(**overrides) -> MaterializationRequest:
    values = dict(
        bucket="ewallet-data-ai-platform-hg26-data",
        dataset_version="canonical-100gb-v1",
        feature_run_id="ab-1-optimized-feat-r1",
        feast_snapshot_id="snapshot-1",
        online_registry_id="online-1",
        online_store_id="canonical-online",
        execution_policy="production",
        redis_secret="feast-online-redis",
        estimated_redis_bytes=10_000,
        target_end=T23,
    )
    return MaterializationRequest(**{**values, **overrides})


def views(end=T22, plan=None, *, source_max=SOURCE_MAX, overrides=None):
    plan = plan or request()
    overrides = overrides or {}
    return [
        ViewState(
            view=name,
            materialized_end=overrides.get(name, end),
            source_path=f"{plan.snapshot_root}/{spec['table']}",
            source_max=source_max,
        )
        for name, spec in FEATURE_SPECS.items()
    ]


def parity(start=T22, end=T23, **overrides):
    stamp = (start + timedelta(minutes=10)).isoformat()
    view_rows = [
        {
            "view": name,
            "entities": ["e1", "e2"],
            "online_event_timestamps": [stamp, stamp],
            "superseded_by_streaming": [],
            "missing_entity_all_null": True,
        }
        for name in FEATURE_SPECS
    ]
    return {
        "status": "PASS",
        "cutover": end.isoformat(),
        "comparisons": 62,
        "mismatch_count": 0,
        "views": view_rows,
        **overrides,
    }


class RequestContractTest(unittest.TestCase):
    def test_paths_match_the_existing_cloud_boundary(self):
        boundary = CloudFeastTrainingBoundary(
            bucket="ewallet-data-ai-platform-hg26-data",
            dataset_version="canonical-100gb-v1",
            silver_run_id="ab-1-optimized-silver-r1",
            feature_run_id="ab-1-optimized-feat-r1",
            snapshot_id="snapshot-1",
            source_git_sha="a" * 40,
            spark_profile="optimized",
            feature_selection_digest="sha256:" + "0" * 64,
        )
        self.assertEqual(boundary.feast_snapshot_root, request().snapshot_root)

    def test_ambiguous_identifiers_are_rejected(self):
        for field in ("feature_run_id", "feast_snapshot_id", "online_registry_id"):
            for value in ("latest", "current", "UPPER", "a"):
                with self.subTest(field=field, value=value), self.assertRaises(WindowError):
                    request(**{field: value})

    def test_a_bucket_must_be_bare(self):
        for value in ("gs://bucket-name", "bucket/name", ""):
            with self.subTest(value=value), self.assertRaises(WindowError):
                request(bucket=value)

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(WindowError):
            request(target_end=datetime(2026, 5, 31, 23))
        with self.assertRaises(WindowError):
            parse_utc("2026-05-31T23:00:00")

    def test_other_offsets_are_normalized_to_utc(self):
        local = datetime(2026, 6, 1, 6, tzinfo=timezone(timedelta(hours=7)))
        self.assertEqual(T23, request(target_end=local).target_end)
        self.assertEqual(request().window_id, request(target_end=local).window_id)

    def test_a_target_after_the_streaming_cutover_is_rejected(self):
        with self.assertRaises(WindowError):
            request(target_end=T00, streaming_cutover=T23)
        self.assertEqual(T23, request(streaming_cutover=T23).target_end)

    def test_window_identity_changes_with_the_snapshot_and_target(self):
        self.assertEqual(request().window_id, request().window_id)
        self.assertNotEqual(request().window_id, request(feast_snapshot_id="snapshot-2").window_id)
        self.assertNotEqual(request().window_id, request(target_end=T00).window_id)

    def test_window_identity_changes_with_the_online_store(self):
        self.assertNotEqual(
            request().window_id,
            request(
                online_store_id="canonical-verify",
                execution_policy="verification",
                redis_secret="feast-online-redis-verify",
            ).window_id,
        )

    def test_registered_dataset_must_use_its_registered_bucket(self):
        with self.assertRaisesRegex(WindowError, "must use bucket"):
            request(bucket="bucket-name")
        rehearsal = request(
            bucket="ewallet-data-ai-platform-hg26-rehearsal-5gb-data",
            dataset_version="rehearsal-5gb-v1",
            online_store_id="rehearsal-online",
            execution_policy="verification",
            redis_secret="feast-online-redis-rehearsal",
        )
        self.assertEqual("rehearsal-5gb-v1", rehearsal.dataset_version)

    def test_rehearsal_refuses_the_production_store_binding(self):
        with self.assertRaisesRegex(WindowError, "rehearsal"):
            request(
                bucket="ewallet-data-ai-platform-hg26-rehearsal-5gb-data",
                dataset_version="rehearsal-5gb-v1",
            )

    def test_store_binding_refuses_an_unapproved_secret(self):
        with self.assertRaisesRegex(WindowError, "secret"):
            request(redis_secret="some-other-secret")

    def test_redis_impact_estimate_must_be_positive(self):
        with self.assertRaisesRegex(WindowError, "estimated_redis_bytes"):
            request(estimated_redis_bytes=0)

    def test_max_window_must_be_positive(self):
        with self.assertRaises(WindowError):
            request(max_window=timedelta(0))


class RedisIsolationTest(unittest.TestCase):
    def verification_request(self, **overrides):
        return request(
            online_store_id="canonical-verify",
            execution_policy="verification",
            redis_secret="feast-online-redis-verify",
            **overrides,
        )

    def evidence(self, plan=None, **overrides):
        plan = plan or self.verification_request()
        run_id = "redis-server-run-1"
        target = "redis.feature-store.svc.cluster.local:6379/1:" + run_id
        fingerprint = "sha256:" + hashlib.sha256(target.encode()).hexdigest()
        values = {
            "logical_id": plan.online_store_id,
            "secret_name": plan.redis_secret,
            "host": "redis.feature-store.svc.cluster.local",
            "port": 6379,
            "db": 1,
            "server_run_id": run_id,
            "target_fingerprint": fingerprint,
            "used_memory_bytes": 1000,
            "maxmemory_bytes": 1_000_000,
            "maxmemory_policy": "noeviction",
            "ownership": {
                "schema_version": "ewallet-redis-campaign-owner-v1",
                "campaign_id": plan.online_registry_id,
                "dataset_version": plan.dataset_version,
                "registry_uri": plan.registry_uri,
                "online_store_id": plan.online_store_id,
                "target_fingerprint": fingerprint,
                "source_run_id": plan.feature_run_id,
                "snapshot_id": plan.feast_snapshot_id,
            },
            "isolation_status": "OWNED",
        }
        return {**values, **overrides}

    def test_valid_isolated_verification_target_passes(self):
        self.assertEqual("PASS", verify_online_store(
            self.verification_request(), self.evidence()
        )["status"])

    def test_incorrect_target_fingerprint_is_rejected(self):
        with self.assertRaisesRegex(WindowError, "fingerprint"):
            verify_online_store(self.verification_request(), self.evidence(target_fingerprint="sha256:bad"))

    def test_wrong_logical_store_identity_is_rejected(self):
        with self.assertRaisesRegex(WindowError, "logical identity"):
            verify_online_store(
                self.verification_request(), self.evidence(logical_id="rehearsal-online")
            )

    def test_missing_or_conflicting_ownership_is_rejected(self):
        plan = self.verification_request()
        for evidence in (
            self.evidence(plan, ownership=None),
            self.evidence(plan, isolation_status="CONFLICT"),
            self.evidence(plan, ownership={**self.evidence(plan)["ownership"], "campaign_id": "other"}),
            self.evidence(plan, ownership={**self.evidence(plan)["ownership"], "registry_uri": request(online_registry_id="online-2").registry_uri}),
        ):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(WindowError, "ownership|isolation"):
                verify_online_store(plan, evidence)

    def rehearsal_evidence(self, database):
        plan = request(
            bucket="ewallet-data-ai-platform-hg26-rehearsal-5gb-data",
            dataset_version="rehearsal-5gb-v1",
            online_store_id="rehearsal-online",
            execution_policy="verification",
            redis_secret="feast-online-redis-rehearsal",
        )
        target = f"redis.feature-store.svc.cluster.local:6379/{database}:redis-server-run-1"
        fingerprint = "sha256:" + hashlib.sha256(target.encode()).hexdigest()
        evidence = self.evidence(plan, db=database, target_fingerprint=fingerprint)
        evidence["ownership"] = {**evidence["ownership"], "target_fingerprint": fingerprint}
        return plan, evidence

    def test_rehearsal_online_accepts_only_its_database_range(self):
        for database in (2, 3, 7):
            with self.subTest(database=database):
                self.assertEqual("PASS", verify_online_store(*self.rehearsal_evidence(database))["status"])
        # DB 1 belongs to canonical-verify (and the RAG canary); 8+ to rehearsal-stream.
        for database in (0, 1, 8, 15):
            with self.subTest(database=database), self.assertRaisesRegex(WindowError, "database"):
                verify_online_store(*self.rehearsal_evidence(database))

    def test_streaming_store_has_its_own_secret_and_disjoint_range(self):
        from ai_platform.features.feast.materialization_plan import ONLINE_STORE_BINDINGS

        online = ONLINE_STORE_BINDINGS["rehearsal-online"]
        stream = ONLINE_STORE_BINDINGS["rehearsal-stream"]
        self.assertEqual("feast-online-redis-stream", stream["redis_secret"])
        self.assertEqual("feast-online-redis-rehearsal", online["redis_secret"])
        self.assertFalse(online["redis_dbs"] & stream["redis_dbs"])
        verify = ONLINE_STORE_BINDINGS["canonical-verify"]
        self.assertFalse(online["redis_dbs"] & verify["redis_dbs"])
        self.assertNotEqual(online["redis_secret"], verify["redis_secret"])
        for binding in ONLINE_STORE_BINDINGS.values():
            if binding["requires_campaign_ownership"]:
                self.assertNotIn(0, binding["redis_dbs"])

    def test_capacity_failure_is_rejected(self):
        with self.assertRaisesRegex(WindowError, "headroom"):
            verify_online_store(
                self.verification_request(estimated_redis_bytes=900_000), self.evidence()
            )

    def test_atomic_claim_accepts_only_new_or_identical_ownership(self):
        class Client:
            def __init__(self, result):
                self.result = result

            def eval(self, script, keys, key, encoded):
                self.call = (script, keys, key, encoded)
                return self.result

        owner = self.evidence()["ownership"]
        for status in ("CLAIMED", b"OWNED"):
            with self.subTest(status=status):
                client = Client(status)
                self.assertIn(claim(client, owner), {"CLAIMED", "OWNED"})
                self.assertIn("DBSIZE", client.call[0])
        for status in ("CONFLICT", "AMBIGUOUS_NONEMPTY"):
            with self.subTest(status=status), self.assertRaisesRegex(RedisIsolationError, status):
                claim(Client(status), owner)


class WindowDecisionTest(unittest.TestCase):
    def test_a_new_window_starts_where_the_registry_stopped(self):
        decision = decide_window(request(), views(T22))
        self.assertEqual(WindowState.NEW_WINDOW, decision.state)
        self.assertEqual(T22, decision.start)
        self.assertEqual(T23, decision.end)
        self.assertEqual(4, len(decision.pending_views))

    def test_repeating_a_finished_window_loads_nothing(self):
        decision = decide_window(request(), views(T23))
        self.assertEqual(WindowState.COMPLETED, decision.state)
        self.assertIsNone(decision.start)

    def test_a_registry_ahead_of_the_target_is_already_complete(self):
        self.assertEqual(WindowState.COMPLETED, decide_window(request(), views(T00)).state)

    def test_views_left_behind_are_recovered_from_their_own_end(self):
        decision = decide_window(request(), views(T23, overrides={"device_behavior": T22}))
        self.assertEqual(WindowState.PARTIAL, decision.state)
        self.assertEqual(("device_behavior",), decision.pending_views)
        self.assertEqual(T22, decision.start)

    def test_a_view_never_materialized_cannot_become_a_full_load(self):
        with self.assertRaisesRegex(WindowError, "full history"):
            decide_window(request(), views(T22, overrides={"user_behavior": None}))
        with self.assertRaisesRegex(WindowError, "full history"):
            decide_window(request(), views(None))

    def test_the_live_registry_past_the_snapshot_reports_no_new_data(self):
        later = datetime(2026, 6, 1, 1, tzinfo=UTC)
        decision = decide_window(request(target_end=later), views(T00))
        self.assertEqual(WindowState.NO_NEW_DATA, decision.state)

    def test_a_target_past_the_snapshot_with_data_left_is_rejected(self):
        with self.assertRaisesRegex(WindowError, "never loaded"):
            decide_window(request(target_end=T00), views(T23))

    def test_a_target_equal_to_the_last_event_is_allowed(self):
        decision = decide_window(request(target_end=SOURCE_MAX), views(T23))
        self.assertEqual(WindowState.NEW_WINDOW, decision.state)

    def test_a_window_above_the_limit_is_rejected(self):
        plan = request(max_window=timedelta(hours=1))
        self.assertEqual(WindowState.NEW_WINDOW, decide_window(plan, views(T22)).state)
        with self.assertRaisesRegex(WindowError, "max_window"):
            decide_window(plan, views(T22 - timedelta(seconds=1)))

    def test_a_missing_or_extra_view_is_rejected(self):
        with self.assertRaisesRegex(WindowError, "drifted"):
            decide_window(request(), views()[:3])
        extra = ViewState("extra", T22, "gs://x", SOURCE_MAX)
        with self.assertRaisesRegex(WindowError, "drifted"):
            decide_window(request(), [*views(), extra])

    def test_a_source_outside_the_snapshot_is_rejected(self):
        other = views(plan=request(feast_snapshot_id="snapshot-2"))
        with self.assertRaisesRegex(WindowError, "not snapshot"):
            decide_window(request(), other)

    def test_a_trailing_slash_on_a_source_is_tolerated(self):
        items = [
            ViewState(view.view, view.materialized_end, view.source_path + "/", view.source_max)
            for view in views()
        ]
        self.assertEqual(WindowState.NEW_WINDOW, decide_window(request(), items).state)


class JobResolutionTest(unittest.TestCase):
    def test_the_first_attempt_creates_a_job(self):
        self.assertEqual(
            (JobAction.CREATE, "feast-inc-abc-a1"), resolve_materialize_job("abc", [])
        )

    def test_a_running_job_is_joined_instead_of_duplicated(self):
        jobs = [ExistingJob("feast-inc-abc-a1", 1, 0, 0)]
        self.assertEqual((JobAction.ATTACH, "feast-inc-abc-a1"), resolve_materialize_job("abc", jobs))

    def test_a_succeeded_job_is_reused(self):
        jobs = [ExistingJob("feast-inc-abc-a1", 0, 0, 1), ExistingJob("feast-inc-abc-a2", 0, 1, 0)]
        self.assertEqual((JobAction.REUSE, "feast-inc-abc-a2"), resolve_materialize_job("abc", jobs))

    def test_a_failed_job_leads_to_the_next_attempt(self):
        jobs = [ExistingJob("feast-inc-abc-a1", 0, 0, 1)]
        self.assertEqual((JobAction.CREATE, "feast-inc-abc-a2"), resolve_materialize_job("abc", jobs))

    def test_two_running_jobs_stop_the_run(self):
        jobs = [ExistingJob("a", 1, 0, 0), ExistingJob("b", 1, 0, 0)]
        with self.assertRaises(WindowError):
            resolve_materialize_job("abc", jobs)

    def test_kubernetes_items_are_converted(self):
        item = {"metadata": {"name": "feast-inc-x-a1"}, "status": {"failed": 1}}
        self.assertEqual(ExistingJob("feast-inc-x-a1", 0, 0, 1), existing_job_from_kubernetes(item))

    def test_job_names_stay_within_the_script_limit(self):
        window = request().window_id
        self.assertLessEqual(len(job_name("state-post", window, 9)), 50)
        with self.assertRaises(WindowError):
            job_name("inc", window, 0)
        with self.assertRaises(WindowError):
            job_name("x" * 40, window, 1)


class CompletionTest(unittest.TestCase):
    def test_every_view_must_reach_the_target(self):
        verify_completion(request(), views(T23))
        with self.assertRaisesRegex(WindowError, "merchant_behavior"):
            verify_completion(request(), views(T23, overrides={"merchant_behavior": T22}))


class ReadbackTest(unittest.TestCase):
    def check(self, evidence):
        return verify_readback(evidence, start=T22, end=T23)

    def test_a_valid_readback_is_summarized(self):
        self.assertEqual(
            {"comparisons": 62, "entities_checked": 8, "entities_superseded_by_streaming": 0},
            self.check(parity()),
        )

    def test_failed_parity_is_rejected(self):
        with self.assertRaises(ReadbackError):
            self.check(parity(status="FAIL"))

    def test_a_vacuous_comparison_is_rejected(self):
        with self.assertRaises(ReadbackError):
            self.check(parity(comparisons=0))

    def test_mismatches_are_rejected_even_with_a_pass_status(self):
        with self.assertRaises(ReadbackError):
            self.check(parity(mismatch_count=1))

    def test_another_cutover_is_rejected(self):
        with self.assertRaisesRegex(ReadbackError, "cutover"):
            self.check(parity(end=T00))

    def test_all_views_are_required(self):
        evidence = parity()
        evidence["views"].pop()
        with self.assertRaisesRegex(ReadbackError, "four"):
            self.check(evidence)

    def test_a_missing_online_row_is_rejected(self):
        evidence = parity()
        evidence["views"][0]["online_event_timestamps"][0] = None
        with self.assertRaisesRegex(ReadbackError, "no online row"):
            self.check(evidence)

    def test_a_timestamp_outside_the_window_is_rejected(self):
        for stamp in (T22 - timedelta(seconds=1), T23 + timedelta(seconds=1)):
            evidence = parity()
            evidence["views"][1]["online_event_timestamps"][1] = stamp.isoformat()
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ReadbackError, "outside"):
                self.check(evidence)

    def test_window_edges_are_inside(self):
        evidence = parity()
        evidence["views"][0]["online_event_timestamps"] = [T22.isoformat(), T23.isoformat()]
        self.assertEqual(8, self.check(evidence)["entities_checked"])

    def test_a_non_null_missing_entity_probe_is_rejected(self):
        evidence = parity()
        evidence["views"][2]["missing_entity_all_null"] = False
        with self.assertRaisesRegex(ReadbackError, "probe"):
            self.check(evidence)

    def test_streaming_owned_entities_are_reported_not_compared(self):
        evidence = parity()
        later = (T23 + timedelta(hours=1)).isoformat()
        evidence["views"][0]["online_event_timestamps"][0] = later
        evidence["views"][0]["superseded_by_streaming"] = ["e1"]
        summary = self.check(evidence)
        self.assertEqual(1, summary["entities_superseded_by_streaming"])
        self.assertEqual(7, summary["entities_checked"])

    def test_a_run_of_only_streaming_owned_entities_proves_nothing(self):
        evidence = parity()
        for item in evidence["views"]:
            item["superseded_by_streaming"] = list(item["entities"])
        with self.assertRaisesRegex(ReadbackError, "no sampled entity"):
            self.check(evidence)


class ProvenanceDigestTest(unittest.TestCase):
    def test_the_digest_ignores_itself_and_detects_edits(self):
        record = {"a": 1}
        digest = provenance_digest(record)
        self.assertEqual(digest, provenance_digest({**record, "provenance_digest": digest}))
        self.assertNotEqual(digest, provenance_digest({"a": 2}))


if __name__ == "__main__":
    unittest.main()
