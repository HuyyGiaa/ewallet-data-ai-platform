# Data Contracts

This directory contains versioned contracts for five high-value datasets on
the transaction and fraud-label paths:

- `silver.transactions`
- `silver.fraud_labels`
- `gold.fact_transactions`
- `gold.obt_transaction_enriched`
- `gold.feat_user_90d`

The YAML files describe stable dataset interfaces: schema, grain, logical
keys, required and optional fields, verified relationships, and quality
rules. They are declarative specifications. The executable enforcement
remains in `data_platform.quality.validate_silver` and
`data_platform.quality.validate_gold`.

The fraud-label contract is part of the canonical Phase 2 lakehouse. Static
YAML validation always includes it, while persisted-data enforcement runs with
`validate_silver --include-fraud-labels`. The default validator remains as a
Phase 1 compatibility mode; canonical validation treats a missing fraud-label
dataset as an error.

The `nullable` values are logical contract expectations. The current Delta
schemas expose permissive `nullable=true` metadata for every field, so the
validators enforce the stricter non-null guarantees against persisted rows.

The project treats the sources in this order:

1. Transformation code defines actual pipeline behavior.
2. These contracts define the intended stable interface and invariants.
3. Validators enforce the executable rules against persisted Delta data.
4. DataHub exposes selected contract checks as assertions.

## Versioning

Every file has `contract_version: 1`. Increment the version when a change
breaks compatibility, including changing grain or key semantics, removing a
required column, making an optional column required, or changing a column to
an incompatible type. Adding an optional column or improving documentation is
non-breaking.

## Validation

The files use ordinary YAML and require no contract framework. From the
repository root, syntax can be checked with the project's existing PyYAML
installation:

```bash
python3 -c "from pathlib import Path; import yaml; [yaml.safe_load(path.read_text()) for path in Path('data_platform/contracts').glob('*.yml')]"
```

Selected `validator_rule` entries are mapped to DataHub assertions. These
contracts do not call DataHub or publish assertions themselves.
