"""Workload Identity probe for the Feature Store's GCS boundary.

The Feast operator, the streaming writers and the validator all run as one
Google service account. Before a verification campaign writes anything, this
probe shows from inside the cluster that the account can:

- read the pinned snapshot manifest (its SHA-256 must match) and list the snapshot;
- create one object in the probe prefix, and only once (create-only, no overwrite);

and that it cannot write to, or list, the prefixes it must never touch.

Run it as a Job with ``scripts/mlops/feature_store_iam_probe.sh``. It prints one
JSON line and exits non-zero unless every check returns the expected result.
No credential or token is read or printed; the identity comes from the
metadata server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

OK = "OK"
FORBIDDEN = "FORBIDDEN"
PRECONDITION_FAILED = "PRECONDITION_FAILED"
ERROR = "ERROR"

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
)
DEFAULT_PROBE_PREFIX = "features/_iam-probe"
DEFAULT_DENIED_WRITE_PREFIXES = ("lakehouse/_iam-negative-probe", "features/offline/_iam-negative-probe")
DEFAULT_DENIED_LIST_PREFIX = "lakehouse/"


@dataclass(frozen=True)
class ProbeConfig:
    """What to check. Every prefix is relative to the bucket root."""

    bucket: str
    snapshot_prefix: str
    expected_manifest_sha256: str
    probe_id: str
    list_view: str = "feat_user_behavior"
    probe_prefix: str = DEFAULT_PROBE_PREFIX
    denied_write_prefixes: Sequence[str] = field(default=DEFAULT_DENIED_WRITE_PREFIXES)
    denied_list_prefix: str = DEFAULT_DENIED_LIST_PREFIX

    def __post_init__(self) -> None:
        if len(self.expected_manifest_sha256) != 64:
            raise ValueError("expected_manifest_sha256 must be 64 hex characters")
        for name in ("bucket", "snapshot_prefix", "probe_id", "probe_prefix"):
            value = getattr(self, name)
            if not value or value.startswith("/") or value.startswith("gs://"):
                raise ValueError(f"{name} must be a non-empty bucket-relative value")


def _classify(error: Exception) -> str:
    # Imported lazily so the module stays importable without the GCS client.
    from google.api_core import exceptions as gexc

    if isinstance(error, gexc.Forbidden):
        return FORBIDDEN
    if isinstance(error, gexc.PreconditionFailed):
        return PRECONDITION_FAILED
    return ERROR


def _check(name: str, action: Callable[[], object], expected: str) -> dict[str, object]:
    try:
        detail: object = action()
        result = OK
    except Exception as error:  # noqa: BLE001 - every outcome is recorded, none is swallowed
        result = _classify(error)
        detail = repr(error)[:300] if result == ERROR else None
    return {"name": name, "result": result, "expected": expected, "pass": result == expected, "detail": detail}


def run_checks(client, config: ProbeConfig) -> dict[str, object]:
    """Run every check with a ``google.cloud.storage.Client``-like ``client``."""

    bucket = client.bucket(config.bucket)
    snapshot = config.snapshot_prefix.rstrip("/")
    probe_object = f"{config.probe_prefix.rstrip('/')}/{config.probe_id}.json"

    def read_manifest() -> str:
        raw = bucket.blob(f"{snapshot}/_control/export_manifest.json").download_as_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != config.expected_manifest_sha256:
            raise ValueError(f"manifest digest {digest} differs from the pinned value")
        return digest

    def list_snapshot() -> int:
        return len(list(client.list_blobs(config.bucket, prefix=f"{snapshot}/{config.list_view}/", max_results=5)))

    def create_once(path: str, payload: str) -> str:
        bucket.blob(path).upload_from_string(payload, if_generation_match=0)
        return path

    checks = [
        _check("read_snapshot_manifest", read_manifest, OK),
        _check("list_snapshot_prefix", list_snapshot, OK),
        _check("create_in_probe_prefix", lambda: create_once(probe_object, json.dumps({"probe": config.probe_id})), OK),
        _check("recreate_same_object_refused", lambda: create_once(probe_object, "x"), PRECONDITION_FAILED),
    ]
    for prefix in config.denied_write_prefixes:
        label = prefix.strip("/").replace("/", "_").replace("-", "_")
        checks.append(
            _check(f"create_denied_{label}", lambda p=prefix: create_once(f"{p.rstrip('/')}/{config.probe_id}", "x"), FORBIDDEN)
        )
    checks.append(
        _check(
            "list_denied",
            lambda: len(list(client.list_blobs(config.bucket, prefix=config.denied_list_prefix, max_results=1))),
            FORBIDDEN,
        )
    )
    status = "PASS" if all(check["pass"] for check in checks) else "FAIL"
    return {"probe_id": config.probe_id, "bucket": config.bucket, "status": status, "checks": checks}


def metadata_identity(timeout: float = 10.0) -> str:
    request = urllib.request.Request(METADATA_IDENTITY_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed metadata URL
        return response.read().decode()


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--snapshot-prefix", required=True, help="bucket-relative snapshot root")
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--probe-prefix", default=DEFAULT_PROBE_PREFIX)
    parser.add_argument("--denied-write-prefix", action="append", dest="denied_write_prefixes")
    parser.add_argument("--denied-list-prefix", default=DEFAULT_DENIED_LIST_PREFIX)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = ProbeConfig(
        bucket=args.bucket,
        snapshot_prefix=args.snapshot_prefix,
        expected_manifest_sha256=args.expected_manifest_sha256,
        probe_id=args.probe_id,
        probe_prefix=args.probe_prefix,
        denied_write_prefixes=tuple(args.denied_write_prefixes or DEFAULT_DENIED_WRITE_PREFIXES),
        denied_list_prefix=args.denied_list_prefix,
    )
    from google.cloud import storage

    report = run_checks(storage.Client(), config)
    report["identity"] = metadata_identity()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
