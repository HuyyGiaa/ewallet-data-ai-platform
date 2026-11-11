"""Airflow-free parameters for the incremental Feast materialization DAG.

Values reach the tasks only as environment variables, so a parameter is never
rendered into Bash source. Secrets are not parameters: the Redis connection stays
in a Kubernetes Secret and is selected by name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DEFAULT_EVIDENCE_ROOT = str(Path.home() / "ewallet-evidence/c1-feast-incremental")
_ID = r"^[a-z0-9][a-z0-9-]{2,127}(?![\s\S])"
_INSTANT = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})(?![\s\S])"

# "(?![\s\S])" is end-of-input in both Python and ECMA-262; "$" would also accept
# a trailing newline.
FEAST_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "mode": {"default": "dry_run", "type": "string", "enum": ["dry_run", "execute"]},
    "approval": {"default": "", "type": "string", "pattern": r"^(?:approve-[0-9a-f]{12})?(?![\s\S])"},
    "data_bucket": {"default": "ewallet-data-ai-platform-hg26-data", "type": "string"},
    "dataset_version": {"default": "canonical-100gb-v1", "type": "string", "pattern": _ID},
    "feature_run_id": {"default": "REQUIRED-feature-run-id", "type": "string", "pattern": _ID},
    "feast_snapshot_id": {"default": "REQUIRED-feast-snapshot-id", "type": "string", "pattern": _ID},
    "online_registry_id": {"default": "REQUIRED-online-registry-id", "type": "string", "pattern": _ID},
    "online_store_id": {
        "default": "canonical-verify",
        "type": "string",
        "enum": ["canonical-online", "canonical-verify", "rehearsal-online"],
    },
    # Zero is deliberately invalid at runtime: the owner must supply an estimate.
    "estimated_redis_bytes": {"default": 0, "type": "integer", "minimum": 0},
    "target_end": {"default": "2026-05-31T23:00:00Z", "type": "string", "pattern": _INSTANT},
    "streaming_cutover": {
        "default": "",
        "type": "string",
        "pattern": r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}))?(?![\s\S])",
    },
    "max_window_hours": {"default": 6, "type": "number", "minimum": 0.25, "maximum": 24},
    "sample_size": {"default": 5, "type": "integer", "minimum": 1, "maximum": 50},
    "expected_manifest_sha256": {"default": "", "type": "string", "pattern": r"^(?:[0-9a-f]{64})?(?![\s\S])"},
    "feast_online_image": {"default": "", "type": "string"},
    "feast_redis_secret": {"default": "feast-online-redis-verify", "type": "string", "pattern": r"^[a-z0-9-]{1,63}(?![\s\S])"},
    "git_sha": {"default": "", "type": "string", "pattern": r"^(?:[0-9a-f]{40})?(?![\s\S])"},
}

FEAST_PARAM_ENV = {
    "mode": "C1_MODE",
    "approval": "C1_APPROVAL",
    "data_bucket": "C1_DATA_BUCKET",
    "dataset_version": "C1_DATASET_VERSION",
    "feature_run_id": "C1_FEATURE_RUN_ID",
    "feast_snapshot_id": "C1_FEAST_SNAPSHOT_ID",
    "online_registry_id": "C1_ONLINE_REGISTRY_ID",
    "online_store_id": "C1_ONLINE_STORE_ID",
    "estimated_redis_bytes": "C1_ESTIMATED_REDIS_BYTES",
    "target_end": "C1_TARGET_END",
    "streaming_cutover": "C1_STREAMING_CUTOVER",
    "max_window_hours": "C1_MAX_WINDOW_HOURS",
    "sample_size": "C1_SAMPLE_SIZE",
    "expected_manifest_sha256": "C1_EXPECTED_MANIFEST_SHA256",
    "feast_online_image": "FEAST_ONLINE_IMAGE",
    "feast_redis_secret": "FEAST_REDIS_SECRET",
    "git_sha": "FEAST_ONLINE_GIT_SHA",
}


def feast_task_environment() -> dict[str, str]:
    """Return task variables; the only place params and run identity are templated."""

    return {
        "C1_RUN_ID": "{{ run_id }}",
        "C1_ATTEMPT": "{{ ti.try_number }}",
        "C1_AIRFLOW_DAG_ID": "{{ dag.dag_id }}",
        # Row 11 is an evidence workflow. This boundary is intentionally not a
        # caller-controlled Airflow parameter.
        "C1_EXECUTION_POLICY": "verification",
        "C1_EVIDENCE_ROOT": os.environ.get("EWALLET_C1_EVIDENCE_ROOT", DEFAULT_EVIDENCE_ROOT),
        **{env: f"{{{{ params.{name} }}}}" for name, env in FEAST_PARAM_ENV.items()},
    }
