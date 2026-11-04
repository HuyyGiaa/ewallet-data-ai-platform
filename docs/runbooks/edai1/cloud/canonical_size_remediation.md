# Canonical 100 GB size remediation

This runbook rewrites only `transactions_v2` shards `00000` through `00034`
for `canonical-100gb-v1`. It reuses the other 33,229 verified Parquet objects.
The approved rewrite is Parquet 2.6 with `compression=NONE`,
`use_dictionary=False`, and `write_statistics=True`.

The tool requires GCS object versioning. `plan` is read-only with respect to
GCS. `apply` and `rollback` require both an immutable plan and an explicit
`--execute` flag. Keep the plan directory and apply ledger until the final
source gate and DP1 have passed.

## USER_RUN environment

Run from the repository root in the `fintech` Conda environment:

```bash
export GCP_PROJECT_ID=ewallet-data-ai-platform-hg26
export EWALLET_DATA_BUCKET=ewallet-data-ai-platform-hg26-data
export REMEDIATION_ROOT="$PWD/tmp/canonical-100gb-v1-size-remediation"

mkdir -p "$REMEDIATION_ROOT"
```

The local filesystem should have at least 2 GB free. The 35 current files are
approximately 228 MB in total; the plan retains their originals, their larger
rewrites, prepared manifests, and the immutable plan. The expected peak local
footprint is below 1 GB, while 2 GB provides operational headroom.

## 1. Read-only plan

```bash
python -m ops.gcp.remediate_canonical_size plan \
  --project "$GCP_PROJECT_ID" \
  --bucket "$EWALLET_DATA_BUCKET" \
  --staging-dir "$REMEDIATION_ROOT" \
  --plan-out "$REMEDIATION_ROOT/remediation-plan.json"
```

Expected result: `PLANNED`, 35 object records, the original generations of the
35 Parquet objects and 35 shard manifests, and
`projected_total_bytes >= 100150000000`. The command downloads and fully
compares only the 35 selected Parquet files. It makes no GCS mutation.

Review the immutable plan before apply:

```bash
python - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["REMEDIATION_ROOT"]) / "remediation-plan.json"
plan = json.loads(path.read_text())
assert plan["status"] == "PLANNED"
assert len(plan["objects"]) == 35
assert [item["shard_id"] for item in plan["objects"]] == list(range(35))
assert plan["projected_total_bytes"] >= 100_150_000_000
print({
    "plan_sha256": plan["plan_sha256"],
    "projected_gain_bytes": plan["projected_gain_bytes"],
    "projected_total_bytes": plan["projected_total_bytes"],
})
PY
```

## 2. Conditional apply

Do not edit or move files under `REMEDIATION_ROOT` between plan and apply.

```bash
python -m ops.gcp.remediate_canonical_size apply \
  --project "$GCP_PROJECT_ID" \
  --bucket "$EWALLET_DATA_BUCKET" \
  --plan "$REMEDIATION_ROOT/remediation-plan.json" \
  --ledger "$REMEDIATION_ROOT/apply-ledger.json" \
  --execute
```

Apply preflights all 71 original generations before its first mutation. It
then uploads exactly 35 Parquet replacements, updates exactly 35 shard
manifests, reads all 3,696 shard manifests to rebuild aggregate metadata, and
uploads `dataset_manifest.json` last. Each write uses `ifGenerationMatch` and
is recorded in the local ledger before and after the request.

Expected apply mutation count is 71: 35 Parquet objects, 35 shard manifests,
and one dataset manifest commit marker. No unaffected Parquet object is read
or written.

## 3. Rollback when apply or validation fails

Rollback is valid after an `APPLIED`, `FAILED`, `ROLLING_BACK`, or
`ROLLBACK_FAILED` ledger state. It refuses a live generation that cannot be
proved to be either the recorded replacement or the original content.

```bash
python -m ops.gcp.remediate_canonical_size rollback \
  --project "$GCP_PROJECT_ID" \
  --bucket "$EWALLET_DATA_BUCKET" \
  --plan "$REMEDIATION_ROOT/remediation-plan.json" \
  --ledger "$REMEDIATION_ROOT/apply-ledger.json" \
  --execute
```

Rollback restores the recorded original Parquet generations first, the
original shard-manifest generations second, and the original dataset manifest
last. A completed rollback is idempotent only while all verified restored
generations remain live.

## Runtime expectation

`plan` reads roughly 228 MB and rewrites 35 files locally; allow about 5–15
minutes depending on GCS bandwidth and local storage. `apply` uploads the
rewritten files and processes 3,696 small shard manifests; allow about 10–25
minutes. A full rollback normally takes about 5–15 minutes. These are planning
estimates, not measured canonical runtimes.
