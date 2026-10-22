# Cloud Silver bounded pilot

Stage 1A runs the existing Silver transformations against the verified R4
Bronze pilot. It does not change Silver business logic and it does not claim a
100 GB or performance result.

## Frozen lineage

- Dataset version: `r4-pilot-1gb-v1`
- Source Bronze run: `r4-bronze-20261022-095608`
- Bronze input root:
  `gs://ewallet-data-ai-platform-hg26-data/lakehouse/runs/r4-bronze-20261022-095608/bronze`
- Silver output: a new immutable
  `gs://ewallet-data-ai-platform-hg26-data/lakehouse/runs/<silver-run-id>/silver`
  prefix.

Never reuse a Silver run ID. The submitter checks that the output prefix has no
objects before an execution request and refuses an existing prefix.

## Dry-run

From the repository root:

```bash
SILVER_RUN_ID="s1a-silver-$(date -u +%Y%m%d-%H%M%S)"

python -m ops.gcp.submit_bronze_to_silver \
  --dataset-version r4-pilot-1gb-v1 \
  --source-bronze-run-id r4-bronze-20261022-095608 \
  --run-id "$SILVER_RUN_ID"
```

The dry-run is local and submits nothing. Review the printed command, roots,
runtime identity, and eight-table selection.

## User execution

After reviewing the dry-run, use the same fresh run ID and add `--execute`:

```bash
python -m ops.gcp.submit_bronze_to_silver \
  --dataset-version r4-pilot-1gb-v1 \
  --source-bronze-run-id r4-bronze-20261022-095608 \
  --run-id "$SILVER_RUN_ID" \
  --execute
```

The expected Delta outputs are `users`, `accounts`, `merchants`, `devices`,
`transactions`, `balance_snapshots`, `login_events`, and `fraud_labels` under
the Silver run root. Transactions retain one logical row per transaction ID
after latest-`ingested_at` deduplication; missing V1 channels become `UNKNOWN`.
Fraud labels remain a separate table and are not joined into transactions.

This runbook stops after Silver. It does not run Gold, fraud features, Feast,
training, or serving.

## Verified bounded run

The bounded pilot completed on Dataproc Serverless 2.2.89:

- Silver run: `s1b-silver-configfix-20261022-174352`
- State: `SUCCEEDED`
- Spark transformation runtime: 326.69 seconds
- Output size: 1,622,083,745 bytes
- Output tables: 8
- Logical transactions: 3,700,000
- Duplicate Bronze transaction rows removed: 74,000
- Silver duplicate transaction IDs: 0
- Silver NULL channels: 0

Delta commit metadata confirms the persisted row counts and schemas. The
transaction schema contains no `label` or `fraud_type`; `fraud_labels` remains
a separate table with exact 3,700,000-row transaction coverage. The local
fraud/Silver regression suite also verifies latest-`ingested_at` retention and
V1 channel normalization semantics.

Machine-readable evidence is recorded in
[`../evidence/cloud/s1b_silver_bounded_pilot.json`](../evidence/cloud/s1b_silver_bounded_pilot.json).
