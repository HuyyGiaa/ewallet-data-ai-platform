"""Executable dry-run contracts for the existing Block A cloud runner."""

from __future__ import annotations

import os
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_BUCKET = "ewallet-data-ai-platform-hg26-data"
LOCAL_HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
).stdout.strip()


class BlockACloudRunnerTest(unittest.TestCase):
    @staticmethod
    def passing_report():
        report = {
            "schema_version": "block-a-consistency-report-v2",
            "status": "PASS",
            "features_compared": 3,
            "entities_compared": 1,
            "comparison": {
                "counts": {"MATCH": 3, "MISSING": 0, "MISMATCH": 0, "NOT_APPLICABLE": 0},
                "timestamp_mismatches": 0,
            },
            "source_event_verification": {"status": "PASS", "comparisons": 3},
            "offline_verification": {"status": "PASS", "rows_retrieved": 1},
            "direct_offline_online_comparison": {
                "status": "PASS", "comparisons": 3, "mismatch_count": 0
            },
        }
        encoded = json.dumps(report, sort_keys=True, default=str).encode("utf-8")
        report["report_digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return report

    def environment(self, namespace="verify-render-example"):
        return {
            **os.environ,
            "FLINK_IMAGE": "example/flink@sha256:" + "a" * 64,
            "SINKS_IMAGE": "example/sinks@sha256:" + "b" * 64,
            "ONLINE_REGISTRY_URI": (
                f"gs://{CANONICAL_BUCKET}/features/registries/canonical-100gb-v1/"
                "online/registry-1/registry.db"
            ),
            "FEAST_OFFLINE_DATA_ROOT": (
                f"gs://{CANONICAL_BUCKET}/streaming-features/campaigns/{namespace}/"
                f"runs/{namespace}/offline"
            ),
            "HISTORY_ROOT": f"gs://{CANONICAL_BUCKET}/streaming-history/{namespace}",
            "OFFLINE_FEATURE_ROOT": f"gs://{CANONICAL_BUCKET}/streaming-features",
            "FEAST_REDIS_SECRET": "feast-online-redis-verify",
            "ONLINE_STORE_ID": "canonical-verify",
            "DATASET_VERSION": "canonical-100gb-v1",
            "FEATURE_RUN_ID": "feature-run-1",
            "FEAST_SNAPSHOT_ID": "snapshot-1",
            "BLOCK_A_ESTIMATED_REDIS_BYTES": "1000",
            "APPROVED_SHA": LOCAL_HEAD,
            "SINKS_IMAGE_SOURCE_SHA": LOCAL_HEAD,
            "FLINK_IMAGE_SOURCE_SHA": LOCAL_HEAD,
        }

    def run_image_validation(self, values, source_values=None):
        environment = {**os.environ, **values, **(source_values or {})}
        script = (
            "source scripts/mlops/lib/common.sh; "
            "require_digest_pinned_images FEATURE_IMAGE SINKS_IMAGE FLINK_IMAGE; "
            "require_matching_source_shas \"$APPROVED_SHA\" "
            "FEATURE_IMAGE_SOURCE_SHA SINKS_IMAGE_SOURCE_SHA FLINK_IMAGE_SOURCE_SHA"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=ROOT, env=environment,
            capture_output=True, text=True, check=False,
        )

    def test_each_required_image_digest_and_source_revision_is_validated_independently(self):
        sha = "1" * 40
        valid = {
            "FEATURE_IMAGE": "example/feature@sha256:" + "a" * 64,
            "SINKS_IMAGE": "example/sinks@sha256:" + "b" * 64,
            "FLINK_IMAGE": "example/flink@sha256:" + "c" * 64,
            "APPROVED_SHA": sha,
        }
        sources = {
            "FEATURE_IMAGE_SOURCE_SHA": sha,
            "SINKS_IMAGE_SOURCE_SHA": sha,
            "FLINK_IMAGE_SOURCE_SHA": sha,
        }
        self.assertEqual(0, self.run_image_validation(valid, sources).returncode)
        for name in ("FEATURE_IMAGE", "SINKS_IMAGE", "FLINK_IMAGE"):
            with self.subTest(image=name):
                invalid = {**valid, name: "example/image:mutable"}
                result = self.run_image_validation(invalid, sources)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(name, result.stderr)
        empty = self.run_image_validation({**valid, "SINKS_IMAGE": ""}, sources)
        self.assertNotEqual(0, empty.returncode)
        self.assertIn("SINKS_IMAGE", empty.stderr)
        mismatch = self.run_image_validation(
            valid, {**sources, "FLINK_IMAGE_SOURCE_SHA": "2" * 40}
        )
        self.assertNotEqual(0, mismatch.returncode)
        self.assertIn("FLINK_IMAGE_SOURCE_SHA", mismatch.stderr)

    def test_render_includes_offline_writer_and_historical_feast_validation(self):
        for namespace in ("verify-fs12-20261111", "verify-render-example"):
            with self.subTest(namespace=namespace), tempfile.TemporaryDirectory() as temporary:
                environment, kubectl_log = self.fake_kubectl(Path(temporary), namespace)
                result = subprocess.run(
                    ["bash", "scripts/streaming/block_a_cloud.sh", "render", namespace],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("name: block-a-offline", result.stdout)
                self.assertIn("data_platform.streaming.sinks.offline_features", result.stdout)
                self.assertIn("--offline-feature-root", result.stdout)
                self.assertIn(f'--campaign-id\", \"{namespace}', result.stdout)
                self.assertIn(environment["FEAST_OFFLINE_DATA_ROOT"], result.stdout)
                self.assertIn("name: EWALLET_OFFLINE_FEATURE_ROOT", result.stdout)
                self.assertIn("name: EWALLET_HISTORY_ROOT", result.stdout)
                self.assertIn(f"ewallet.block-a/namespace: {namespace}", result.stdout)
                if namespace != "verify-render-example":
                    self.assertNotIn("ewallet.block-a/namespace: verify-render-example", result.stdout)
                self.assertFalse(kubectl_log.exists())
                self.assertFalse((Path(temporary) / "preflight").exists())

    def test_render_requires_one_valid_namespace_matching_all_storage_paths(self):
        command = ["bash", "scripts/streaming/block_a_cloud.sh", "render"]
        missing = subprocess.run(
            command, cwd=ROOT, env=self.environment(), capture_output=True, text=True
        )
        self.assertNotEqual(0, missing.returncode)
        self.assertIn("namespace", missing.stderr)

        invalid = subprocess.run(
            [*command, "production"], cwd=ROOT, env=self.environment("production"),
            capture_output=True, text=True,
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("invalid verification namespace", invalid.stderr)

        namespace = "verify-fs12-20261111"
        environment = self.environment(namespace)
        for changed in (
            {"FEAST_OFFLINE_DATA_ROOT": environment["FEAST_OFFLINE_DATA_ROOT"].replace(f"campaigns/{namespace}", "campaigns/verify-other")},
            {"FEAST_OFFLINE_DATA_ROOT": environment["FEAST_OFFLINE_DATA_ROOT"].replace(f"runs/{namespace}", "runs/verify-other")},
            {"HISTORY_ROOT": environment["HISTORY_ROOT"].replace(namespace, "verify-other")},
            {"OFFLINE_FEATURE_ROOT": "gs://other-bucket/streaming-features"},
        ):
            with self.subTest(changed=changed):
                result = subprocess.run(
                    [*command, namespace], cwd=ROOT, env={**environment, **changed},
                    capture_output=True, text=True,
                )
                self.assertNotEqual(0, result.returncode)

    def test_campaign_identity_is_consistent_through_render_preflight_and_run(self):
        namespace = "verify-fs12-identity"
        environment = self.environment(namespace)
        commands = (
            ("render", namespace),
            ("preflight", namespace),
            ("run", namespace),
        )
        for action, identity in commands:
            with self.subTest(action=action):
                result = subprocess.run(
                    ["bash", "scripts/streaming/block_a_cloud.sh", action, identity],
                    cwd=ROOT, env=environment, capture_output=True, text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn("verify-render-example", result.stdout + result.stderr)

    def test_storage_validation_fails_before_kubectl_for_wrong_or_ambiguous_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, log = self.fake_kubectl(Path(temporary))
            cases = (
                {"DATASET_VERSION": "rehearsal-5gb-v1", "ONLINE_STORE_ID": "rehearsal-online"},
                {"HISTORY_ROOT": f"gs://{CANONICAL_BUCKET}/streaming-history/../escape"},
                {"OFFLINE_FEATURE_ROOT": "gs://other-bucket/streaming-features"},
            )
            for changed in cases:
                with self.subTest(changed=changed):
                    result = subprocess.run(
                        ["bash", "scripts/streaming/block_a_cloud.sh", "preflight", "verify-render-example"],
                        cwd=ROOT,
                        env={**environment, **changed},
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(0, result.returncode)
            self.assertFalse(log.exists())

    def fake_kubectl(self, root: Path, namespace="verify-render-example") -> tuple[dict[str, str], Path]:
        binary = root / "kubectl"
        log = root / "kubectl.log"
        binary.write_text(
            """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"${FAKE_KUBECTL_LOG}"
case "$*" in
  *" apply -f -"*) cat >/dev/null ;;
  *"wait"*"job/block-a-validate"*) exit "${FAKE_VALIDATOR_WAIT_STATUS:-0}" ;;
  *"logs job/block-a-validate"*)
    [[ "${FAKE_VALIDATOR_LOG_STATUS:-0}" == 0 ]] || exit "${FAKE_VALIDATOR_LOG_STATUS}"
    printf 'EWALLET_CONSISTENCY_REPORT=%s\n' "${FAKE_VALIDATOR_REPORT:-}"
    ;;
  *"exec deploy/block-a-flink-jobmanager"*"jobs/overview"*) printf '%s\n' '{"jobs":[]}' ;;
  *"get job/"*"ignore-not-found"*)
    case "${FAKE_JOB_RESOURCE_STATE:-${FAKE_RESOURCE_STATE:-absent}}" in
      absent) ;;
      same) printf 'resource|%s' "${FAKE_EXPECTED_CAMPAIGN}" ;;
      foreign) printf 'resource|verify-foreign' ;;
      missing-label) printf 'resource|' ;;
    esac
    ;;
  *"get service/"*"ignore-not-found"*|*"get deployment/"*"ignore-not-found"*|*"get configmap/"*"ignore-not-found"*|*"get job/"*"ignore-not-found"*)
    case "${FAKE_RESOURCE_STATE:-absent}" in
      absent) ;;
      same) printf 'resource|%s' "${FAKE_EXPECTED_CAMPAIGN}" ;;
      partial) if [[ "$*" == *"deployment/block-a-redpanda"* ]]; then printf 'resource|%s' "${FAKE_EXPECTED_CAMPAIGN}"; fi ;;
      foreign) printf 'resource|verify-foreign' ;;
      missing-label) printf 'resource|' ;;
    esac
    ;;
  *"rollout status"*) exit "${FAKE_ROLLOUT_STATUS:-0}" ;;
  *"get secret"*"data.connection_string"*) printf '%s' 'cmVkaXMuZmVhdHVyZS1zdG9yZS5zdmMuY2x1c3Rlci5sb2NhbDo2Mzc5LGRiPTE=' ;;
  *"get secret"*"metadata.resourceVersion"*) printf '%s' 'resource-version-1' ;;
  *"INFO memory"*) printf 'used_memory:1000\\r\\nmaxmemory:1000000\\r\\nmaxmemory_policy:noeviction\\r\\n' ;;
  *"INFO server"*) printf 'run_id:server-run-1\\r\\n' ;;
  *" EVAL "*) status="${FAKE_CLAIM_STATUS:-CLAIMED}"; if [[ "$status" == CLAIMED || "$status" == OWNED ]]; then printf '%s' "${@: -1}" >"${FAKE_REDIS_OWNER}"; fi; printf '%s\\n' "$status" ;;
  *" GET "*) cat "${FAKE_REDIS_OWNER}" ;;
esac
""",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        return {
            **self.environment(namespace),
            "PATH": f"{root}:{os.environ['PATH']}",
            "FAKE_KUBECTL_LOG": str(log),
            "FAKE_REDIS_OWNER": str(root / "redis-owner"),
            "BLOCK_A_PREFLIGHT_ROOT": str(root / "preflight"),
        }, log

    def completed_preflight(self, environment, namespace):
        environment["FAKE_EXPECTED_CAMPAIGN"] = namespace
        result = subprocess.run(
            [
                "bash", "scripts/streaming/block_a_cloud.sh", "preflight", namespace,
                "--execute", "--approve-redis-claim",
            ],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_foreign_or_unlabelled_kubernetes_resources_block_apply_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace = "verify-owner"
            environment, log = self.fake_kubectl(Path(temporary), namespace)
            self.completed_preflight(environment, namespace)
            command = ["bash", "scripts/streaming/block_a_cloud.sh"]
            for state in ("foreign", "missing-label"):
                log.write_text("", encoding="utf-8")
                for action in ("deploy", "run", "cleanup"):
                    with self.subTest(state=state, action=action):
                        result = subprocess.run(
                            [*command, action, namespace, "--execute"],
                            cwd=ROOT,
                            env={**environment, "FAKE_RESOURCE_STATE": state},
                            capture_output=True,
                            text=True,
                        )
                        self.assertNotEqual(0, result.returncode)
                self.assertNotIn(" apply -f -", log.read_text(encoding="utf-8"))
                self.assertNotIn(" delete ", log.read_text(encoding="utf-8"))

    def test_empty_or_same_campaign_resources_allow_safe_retry(self):
        for state in ("absent", "same", "partial"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                namespace = "verify-retry"
                environment, log = self.fake_kubectl(Path(temporary), namespace)
                self.completed_preflight(environment, namespace)
                result = subprocess.run(
                    ["bash", "scripts/streaming/block_a_cloud.sh", "deploy", namespace, "--execute"],
                    cwd=ROOT,
                    env={**environment, "FAKE_RESOURCE_STATE": state},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(" apply -f -", log.read_text(encoding="utf-8"))

    def test_failed_deployment_returns_nonzero_without_automatic_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace = "verify-deploy-fail"
            environment, log = self.fake_kubectl(Path(temporary), namespace)
            self.completed_preflight(environment, namespace)
            result = subprocess.run(
                ["bash", "scripts/streaming/block_a_cloud.sh", "deploy", namespace, "--execute"],
                cwd=ROOT,
                env={**environment, "FAKE_ROLLOUT_STATUS": "1"},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, result.returncode)
            calls = log.read_text(encoding="utf-8")
            self.assertIn(" apply -f -", calls)
            self.assertNotIn(" delete ", calls)

    def test_existing_same_campaign_job_cannot_be_reused_as_fresh_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace = "verify-job-reuse"
            environment, log = self.fake_kubectl(Path(temporary), namespace)
            self.completed_preflight(environment, namespace)
            log.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", "scripts/streaming/block_a_cloud.sh", "run", namespace, "--execute"],
                cwd=ROOT,
                env={
                    **environment,
                    "FAKE_RESOURCE_STATE": "same",
                    "FAKE_JOB_RESOURCE_STATE": "same",
                },
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("already exists", result.stderr)
            self.assertNotIn(" apply -f -", log.read_text(encoding="utf-8"))

    def test_run_requires_the_owned_stack_before_creating_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace = "verify-missing-stack"
            environment, log = self.fake_kubectl(Path(temporary), namespace)
            self.completed_preflight(environment, namespace)
            log.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", "scripts/streaming/block_a_cloud.sh", "run", namespace, "--execute"],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("required Kubernetes resource", result.stderr)
            self.assertNotIn(" apply -f -", log.read_text(encoding="utf-8"))

    def test_collect_propagates_validator_and_report_failures_after_preserving_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace = "verify-collect"
            environment, _ = self.fake_kubectl(Path(temporary), namespace)
            environment["FAKE_RESOURCE_STATE"] = "same"
            environment["FAKE_JOB_RESOURCE_STATE"] = "absent"
            evidence = Path(temporary) / "evidence"
            environment["BLOCK_A_EVIDENCE_ROOT"] = str(evidence)
            self.completed_preflight(environment, namespace)
            command = [
                "bash", "scripts/streaming/block_a_cloud.sh", "collect", namespace, "--execute"
            ]
            valid = json.dumps(self.passing_report(), separators=(",", ":"))
            passed = subprocess.run(
                command, cwd=ROOT, env={**environment, "FAKE_VALIDATOR_REPORT": valid},
                capture_output=True, text=True,
            )
            self.assertEqual(0, passed.returncode, passed.stderr)
            run = evidence / namespace
            self.assertTrue((run / "consistency-report.log").exists())
            self.assertTrue((run / "consistency-report.json").exists())
            self.assertTrue((run / "validator-job.json").exists())

            cases = (
                {"FAKE_VALIDATOR_WAIT_STATUS": "1", "FAKE_VALIDATOR_REPORT": valid},
                {"FAKE_VALIDATOR_REPORT": ""},
                {"FAKE_VALIDATOR_REPORT": "{malformed"},
                {"FAKE_VALIDATOR_REPORT": json.dumps({
                    **self.passing_report(), "report_digest": "sha256:" + "a" * 64,
                })},
                {"FAKE_VALIDATOR_REPORT": json.dumps({**self.passing_report(), "status": "FAIL"})},
                {"FAKE_VALIDATOR_REPORT": json.dumps({**self.passing_report(), "features_compared": 0})},
                {"FAKE_VALIDATOR_REPORT": json.dumps({
                    **self.passing_report(),
                    "direct_offline_online_comparison": {
                        "status": "FAIL", "comparisons": 3, "mismatch_count": 1
                    },
                })},
                {"FAKE_VALIDATOR_REPORT": valid, "FAKE_VALIDATOR_LOG_STATUS": "1"},
            )
            for changed in cases:
                with self.subTest(changed=changed):
                    result = subprocess.run(
                        command, cwd=ROOT, env={**environment, **changed},
                        capture_output=True, text=True,
                    )
                    self.assertNotEqual(0, result.returncode)
                    self.assertTrue((run / "flink-jobs.json").exists())
                    self.assertTrue((run / "consistency-report.log").exists())
                    self.assertTrue((run / "consistency-report.json").exists())

    def test_execute_requires_a_matching_preflight_and_cleanup_is_campaign_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, log = self.fake_kubectl(Path(temporary), "verify-campaign-1")
            command = ["bash", "scripts/streaming/block_a_cloud.sh"]
            blocked = subprocess.run(
                [*command, "cleanup", "verify-campaign-1", "--execute"],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn("missing successful preflight", blocked.stderr)

            failed = subprocess.run(
                [*command, "preflight", "verify-campaign-1", "--execute", "--approve-redis-claim"],
                cwd=ROOT, env={**environment, "FAKE_CLAIM_STATUS": "CONFLICT"},
                capture_output=True, text=True,
            )
            self.assertNotEqual(0, failed.returncode)
            self.assertFalse((Path(temporary) / "preflight" / "verify-campaign-1.preflight").exists())

            preflight = subprocess.run(
                [*command, "preflight", "verify-campaign-1", "--execute", "--approve-redis-claim"],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(0, preflight.returncode, preflight.stderr)
            changed = {**environment, "ONLINE_REGISTRY_URI": environment["ONLINE_REGISTRY_URI"].replace("/registry-1/", "/other/")}
            mismatch = subprocess.run(
                [*command, "cleanup", "verify-campaign-1", "--execute"],
                cwd=ROOT, env=changed, capture_output=True, text=True,
            )
            self.assertNotEqual(0, mismatch.returncode)
            self.assertIn("stale or mismatched", mismatch.stderr)

            allowed = subprocess.run(
                [*command, "cleanup", "verify-campaign-1", "--execute"],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(0, allowed.returncode, allowed.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertIn(
                "app.kubernetes.io/part-of=ewallet-block-a,ewallet.block-a/namespace=verify-campaign-1",
                calls,
            )

    def test_redis_claim_requires_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace = "verify-approval"
            environment, log = self.fake_kubectl(Path(temporary), namespace)
            result = subprocess.run(
                ["bash", "scripts/streaming/block_a_cloud.sh", "preflight", namespace, "--execute"],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("explicit --approve-redis-claim", result.stderr)
            self.assertFalse(log.exists())

    def test_dry_run_never_invokes_kubectl(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, log = self.fake_kubectl(Path(temporary), "verify-campaign-2")
            result = subprocess.run(
                ["bash", "scripts/streaming/block_a_cloud.sh", "cleanup", "verify-campaign-2"],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(log.exists())

    def test_shell_entrypoints_are_syntactically_valid(self):
        for path in (
            "scripts/streaming/block_a_cloud.sh",
            "scripts/streaming/block_a_verify.sh",
            "scripts/mlops/feast_online_job.sh",
        ):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["bash", "-n", path], cwd=ROOT, capture_output=True, text=True
                )
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
