"""Provision and verify the dedicated rehearsal data bucket.

Three subcommands:

* ``plan`` prints the exact owner-reviewable commands; it changes nothing.
* ``check`` is read-only and exits non-zero unless the bucket is ready.
* ``apply`` creates only this bucket and grants only the Spark service account.
  It runs only with ``--execute`` and ``--confirm-bucket`` naming the exact bucket.

The bucket settings match the canonical data bucket (uniform access, public access
prevented, object versioning). The canonical bucket is never touched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_platform.config.dataset_identity import CANONICAL_DATA_BUCKET, REHEARSAL_5GB, resolve_storage_context
from ops.gcp.submit_silver_to_gold import _load_outputs
from ops.gcp.submit_source_to_bronze import load_cloud_runtime


LOCATION = "ASIA-SOUTHEAST1"
SPARK_ROLE = "roles/storage.objectUser"  # the Spark account's role on the canonical bucket


class StorageNotReady(ValueError):
    """The rehearsal bucket is absent or does not meet its contract."""


@dataclass(frozen=True)
class StoragePlan:
    project_id: str
    bucket: str
    spark_service_account: str

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}"

    @property
    def spark_member(self) -> str:
        return f"serviceAccount:{self.spark_service_account}"


def provisioning_commands(plan: StoragePlan) -> tuple[list[str], ...]:
    if plan.bucket == CANONICAL_DATA_BUCKET:
        raise ValueError("the canonical bucket is never provisioned here")
    return (
        ["gcloud", "storage", "buckets", "create", plan.uri, f"--project={plan.project_id}",
         f"--location={LOCATION}", "--default-storage-class=STANDARD",
         "--uniform-bucket-level-access", "--public-access-prevention"],
        ["gcloud", "storage", "buckets", "update", plan.uri, "--versioning"],
        ["gcloud", "storage", "buckets", "add-iam-policy-binding", plan.uri,
         f"--member={plan.spark_member}", f"--role={SPARK_ROLE}"],
    )


def describe_bucket(plan: StoragePlan, runner: Callable[..., Any]) -> Mapping[str, Any] | None:
    """Return the bucket metadata, None when it does not exist; other errors raise."""

    completed = runner(["gcloud", "storage", "buckets", "describe", plan.uri, "--format=json"],
                       check=False, text=True, capture_output=True)
    if completed.returncode == 0:
        return json.loads(completed.stdout)
    message = f"{completed.stdout}\n{completed.stderr}".lower()
    if "not found" in message or "404" in message:
        return None
    raise StorageNotReady(f"could not inspect {plan.uri}: {completed.stderr.strip()}")


def check_bucket(plan: StoragePlan, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Read-only readiness check; raises ``StorageNotReady`` listing every problem."""

    bucket = describe_bucket(plan, runner)
    if bucket is None:
        raise StorageNotReady(f"GCS_PUBLICATION_BLOCKED_BY_BUCKET_PROVISIONING: {plan.uri} does not exist")
    problems = []
    if str(bucket.get("location", "")).upper() != LOCATION:
        problems.append(f"location is {bucket.get('location')}, expected {LOCATION}")
    if bucket.get("uniform_bucket_level_access") is not True:
        problems.append("uniform bucket-level access is off")
    if bucket.get("public_access_prevention") != "enforced":
        problems.append("public access prevention is not enforced")
    if bucket.get("versioning_enabled") is not True:
        problems.append("object versioning is off")
    completed = runner(["gcloud", "storage", "buckets", "get-iam-policy", plan.uri, "--format=json"],
                       check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise StorageNotReady(f"could not read IAM of {plan.uri}: {completed.stderr.strip()}")
    bindings = json.loads(completed.stdout).get("bindings", [])
    if any(member in ("allUsers", "allAuthenticatedUsers")
           for binding in bindings for member in binding.get("members", [])):
        problems.append("the bucket has a public IAM binding")
    if not any(binding.get("role") == SPARK_ROLE and plan.spark_member in binding.get("members", [])
               and not binding.get("condition") for binding in bindings):
        problems.append(f"{plan.spark_member} does not hold {SPARK_ROLE}")
    if problems:
        raise StorageNotReady(f"{plan.uri}: " + "; ".join(problems))
    return {"bucket": plan.bucket, "location": LOCATION, "spark_member": plan.spark_member, "status": "READY"}


def plan_for(dataset_version: str, outputs: Mapping[str, Any]) -> StoragePlan:
    context = resolve_storage_context(dataset_version)
    runtime = load_cloud_runtime(outputs)
    return StoragePlan(runtime.project_id, context.data_bucket, runtime.service_account_email)


def apply_plan(plan: StoragePlan, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Create or complete the bucket, skipping what exists, then verify it."""

    commands = provisioning_commands(plan)
    if describe_bucket(plan, runner) is None:
        runner(commands[0], check=True)
    for command in commands[1:]:
        runner(command, check=True)
    return check_bucket(plan, runner)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "check", "apply"))
    parser.add_argument("--dataset-version", default=REHEARSAL_5GB)
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--project-id", help="With --spark-service-account, skips Terraform outputs.")
    parser.add_argument("--spark-service-account", help="With --project-id, skips Terraform outputs.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-bucket", default="")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, runner: Callable[..., Any] = subprocess.run) -> int:
    args = parse_args(argv)
    if args.project_id and args.spark_service_account:
        plan = StoragePlan(args.project_id, resolve_storage_context(args.dataset_version).data_bucket,
                           args.spark_service_account)
    else:
        try:
            plan = plan_for(args.dataset_version, _load_outputs(args.terraform_output_json, runner))
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            print("REFUSED: cannot read Terraform outputs; pass --terraform-output-json or "
                  f"--project-id with --spark-service-account ({error})", file=sys.stderr)
            return 1
    if plan.bucket == CANONICAL_DATA_BUCKET:
        print(f"REFUSED: {args.dataset_version} uses the canonical bucket", file=sys.stderr)
        return 1
    if args.command == "plan":
        for command in provisioning_commands(plan):
            print(" ".join(command))
        return 0
    if args.command == "apply":
        if not args.execute or args.confirm_bucket != plan.bucket:
            print(f"REFUSED: apply needs --execute --confirm-bucket {plan.bucket}", file=sys.stderr)
            return 1
        print(json.dumps(apply_plan(plan, runner), indent=2, sort_keys=True))
        return 0
    try:
        print(json.dumps(check_bucket(plan, runner), indent=2, sort_keys=True))
    except StorageNotReady as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
