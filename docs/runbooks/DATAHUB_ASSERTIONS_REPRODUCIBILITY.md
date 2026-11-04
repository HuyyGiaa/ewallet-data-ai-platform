# DataHub assertions: reproducibility and evidence rerun

This runbook publishes only results produced by an executed validation rule. It
does not rebuild the existing lineage graph or the 31 Cloud Delta Metadata
Contract assertions. Run it from a clean checkout of the merged `main` commit.

## Runtime contract

- CPython: `>=3.10,<3.11` (tested with 3.10.22).
- uv: 0.13.5.
- DataHub SDK: `acryl-datahub==1.7.0.5` from the committed lock.
- DataHub server: tested against the pinned v1.7.0.1 local runtime.
- Configuration: `DATAHUB_GMS_TOKEN` is optional when local GMS auth is off.
  GCP validation uses the existing `configs/environments/gcp/platform.yaml`
  contract and its documented environment variables.

Never put a token in a command line, log or evidence artifact.

## 1. Clean-main and locked setup

```bash
git checkout main
git pull --ff-only origin main
git rev-parse HEAD

UV=/home/huyy-giaa/miniconda3/envs/fintech/bin/uv
"$UV" --version
"$UV" lock --check --project data_platform/metadata/datahub
"$UV" sync --locked --project data_platform/metadata/datahub
"$UV" run --locked --project data_platform/metadata/datahub -- \
  python -c 'import datahub,sys; print(sys.version, datahub.__version__)'
```

If `uv` is installed elsewhere, set `UV` to `command -v uv`. Do not regenerate
the lock during a reproduction run.

## 2. Connectivity check

Starting the pinned local runtime is optional and must pass the host resource
gate first. It preserves its Docker volumes when stopped.

```bash
export DATAHUB_PYTHON="$PWD/data_platform/metadata/datahub/.venv/bin/python"
export DATAHUB_PULL_IMAGES=false
export DATAHUB_MAPPED_GMS_PORT="${DATAHUB_MAPPED_GMS_PORT:-18080}"
export DATAHUB_GMS_URL="http://localhost:${DATAHUB_MAPPED_GMS_PORT}"
./data_platform/metadata/datahub/runtime/start_datahub.sh start
./data_platform/metadata/datahub/runtime/start_datahub.sh check
curl --fail --silent "$DATAHUB_GMS_URL/config" | python -m json.tool
```

Use the actual GMS URL returned by the selected deployment. Do not point the
publisher at Airflow's UI/API port.

## 3. HISTORICAL_VALIDATION_RESULT_PUBLICATION

This procedure replays already executed validation evidence. It does not run
DP1, DP2, DP3, Spark, Dataproc or Airflow. Approved source evidence lives under
`$HOME/ewallet-evidence/edai1-final-evidence/` and
`$HOME/ewallet-evidence/feast-pit/`. Access requires the original evidence disk;
no cloud credential is required merely to recover these local artifacts.

The recovered eight-outcome artifact must have SHA-256
`d57fb6dca057d038c79033d3e00370fb92b6a1cc0d7a5d95984fc391e9c958de`.
The corresponding dry-run plan must have SHA-256
`c3e3384a285b260078023a4c2446e599eab0f09373df76b68681a60336c04d28`.
Stop if either digest differs. A new validation campaign belongs in its own
runbook and requires a separate compute/cost authorization.

## 4. Recover result-bound assertions from existing real artifacts

Set paths to immutable artifacts from the same dataset lineage. The following
development evidence is known to be mutually consistent:

```bash
EVIDENCE_ROOT="$HOME/ewallet-evidence/datahub-assertions-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE_ROOT"

"$UV" run --locked --project data_platform/metadata/datahub -- \
  python -m data_platform.metadata.datahub.assertions.recover_validation_results \
  --inventory "$HOME/ewallet-evidence/edai1-final-evidence/datahub/verified_cloud_inventory.json" \
  --dp1-airflow-log "$HOME/ewallet-evidence/edai1-final-evidence/airflow/dp1/historical-logs/ab-20261025134342-optimized-dp1-r261025174525/submit_bronze_dataproc/attempt=1.log" \
  --dp2-airflow-log "$HOME/ewallet-evidence/edai1-final-evidence/airflow/dp2/historical-logs/ab-20261025134342-optimized-dp2-r261025181923/submit_silver_dataproc/attempt=1.log" \
  --dp3-pit-json "$HOME/ewallet-evidence/feast-pit/feast-g3-261030183547/pit-validation.json" \
  --dp3-execution-json "$HOME/ewallet-evidence/feast-pit/feast-g3-261030183547/cloudrun-execute.json" \
  --generated-at "2026-10-31T21:35:54.049517Z" \
  --output "$EVIDENCE_ROOT/validation-results.json"
```

Expected development result: eight outcomes — seven executed PASS outcomes and
one DP2 Gold `NOT_EXECUTED`. The latter must not produce a run event.

```bash
test "$(sha256sum "$EVIDENCE_ROOT/validation-results.json" | cut -d' ' -f1)" = \
  d57fb6dca057d038c79033d3e00370fb92b6a1cc0d7a5d95984fc391e9c958de
```

## 5. Dry-run, publish and raw readback

```bash
"$UV" run --locked --project data_platform/metadata/datahub -- \
  python -m data_platform.metadata.datahub.assertions.result_bound_publisher \
  --validation-artifact "$EVIDENCE_ROOT/validation-results.json" \
  --dry-run --output "$EVIDENCE_ROOT/datahub-publication-plan.json"

"$UV" run --locked --project data_platform/metadata/datahub -- \
  python -m data_platform.metadata.datahub.assertions.result_bound_publisher \
  --validation-artifact "$EVIDENCE_ROOT/validation-results.json" \
  --publish --gms-url "$DATAHUB_GMS_URL" \
  --output "$EVIDENCE_ROOT/datahub-publish-readback.json"

"$UV" run --locked --project data_platform/metadata/datahub -- \
  python -m data_platform.metadata.datahub.assertions.result_bound_publisher \
  --validation-artifact "$EVIDENCE_ROOT/validation-results.json" \
  --publish --gms-url "$DATAHUB_GMS_URL" \
  --output "$EVIDENCE_ROOT/datahub-publish-readback-idempotency.json"
```

The publish command succeeds only after `assertionInfo` and the matching
`assertionRunEvent` agree on assertion URN, target dataset URN, rule, result,
timestamp and run provenance. The output retains raw REST responses as well as
normalized semantic readback. Re-running the same artifact uses the same
assertion URN, run ID and execution timestamp. GMS 1.7 does not reliably apply
the server-side `runId` timeseries filter, so the publisher intentionally saves
the unfiltered raw history and matches the exact deterministic run ID on the
client. The second output must still report eight definitions, seven run events,
one raw value per executed assertion and zero raw values for Gold.

## 6. Tests and evidence capture

```bash
"$UV" run --locked --project data_platform/metadata/datahub -- \
  pytest -q tests/unit/test_datahub_result_assertions.py \
           tests/unit/test_uv_dependency_contract.py \
           tests/unit/test_feast_metadata_runtime_config.py
git rev-parse HEAD > "$EVIDENCE_ROOT/git-head.txt"
git status --porcelain=v1 > "$EVIDENCE_ROOT/git-status.txt"
date -u --iso-8601=seconds > "$EVIDENCE_ROOT/captured-at.txt"
find "$EVIDENCE_ROOT" -type f ! -name SHA256SUMS -print0 | sort -z | \
  xargs -0 sha256sum > "$EVIDENCE_ROOT/SHA256SUMS"
```

After publication, capture the actual DataHub assertion list/detail UI showing
the dataset, rule and latest run. Store PNGs beside the JSON and record the URL,
timestamp and assertion URN in a text file. A screenshot alone does not replace
the raw readback JSON.

Organize approved screenshots according to
[`docs/evidence/images/datahub/README.md`](../evidence/images/datahub/README.md)
and explain their claim boundaries in
[`docs/edai1/DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md`](../edai1/DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md).
The committed image set is a historical documentation snapshot, not proof of a
post-merge rerun.

Record the official merged-main identity immediately before reproduction:

```bash
git branch --show-current
git rev-parse HEAD | tee "$EVIDENCE_ROOT/merged-main-git-head.txt"
git status --porcelain=v1 | tee "$EVIDENCE_ROOT/merged-main-git-status.txt"
```

Do not fill or infer that SHA from this feature branch. `POST_MERGE_REPRODUCED`
remains false until these commands and the publication/readback procedure are
run from the actual merged `main` commit.

## 7. Pass criteria and cleanup

- Lock check and locked sync succeed.
- Eight definitions read back; only seven executed outcomes have run events.
- Seven executed outcomes retain their original timestamps/run IDs/digests.
- DP2 Gold remains `NOT_EXECUTED` until a real row-level output exists.
- Published definition and run read back semantically; raw responses are saved.
- Existing lineage identities and 31 metadata assertions are unchanged.

Stop only the DataHub runtime started by this operator and this run; keep
volumes. Never stop a runtime owned by another operator:

```bash
./data_platform/metadata/datahub/runtime/start_datahub.sh stop
```

Never use DataHub `nuke`, delete assertion entities, or remove shared Docker
volumes during evidence cleanup.

After the feature is actually merged, repeat this run from the exact `main` SHA
and create a new evidence directory. Until then the correct status is
`POST_MERGE_REPRODUCED = BLOCKED`, not complete.
