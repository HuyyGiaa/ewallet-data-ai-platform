# DataHub evidence images

This directory is a documentation snapshot of **existing** DataHub UI evidence.
The files were copied byte-for-byte from their original evidence stores; no
image was cropped, retouched or enhanced. `IMAGE_MANIFEST.csv` records the
absolute source, SHA-256, runtime identity and narrow claim each image supports.

## Inventory

- `lineage/`: five historical cloud-derived lineage/DataFlow screenshots.
- `assertions/`: one current publication of a historical DP1 result-bound
  assertion and three historical metadata-contract screenshots.
- `datasets_schema/`: one historical Feature dataset schema screenshot.

Total: **10 screenshots** (5 lineage, 4 assertions, 1 dataset/schema).

The raw API/readback bundle remains outside Git at
`/home/huyy-giaa/ewallet-evidence/datahub-assertions-20261101/runtime-20261031T220705Z/`.
It contains eight assertion definitions, seven evidence-backed PASS run events,
Gold zero-event proof, semantic readback and raw API responses. Images alone do
not replace those machine-readable records.

## Limitations

- No new DP2 Silver, DP2 Gold or DP3 PIT assertion UI screenshot exists.
- DP2 Gold is `NOT_EXECUTED`; no PASS run event or PASS screenshot exists.
- The three Feature assertion images are older Cloud Delta Metadata Contract
  evidence. They must not be described as the four new PIT assertions.
- The schema image shows `event_timestamp` but no `created` field.
- `docs/evidence/datahub/01_transaction_lineage.png` remains preserved at its
  original path. It was not copied because its run identity is unknown and
  newer cloud-run-specific DP2 screenshots are available.
- `feature_quality.png` was excluded because its visible “No runs/Running”
  summary conflicts with the more specific historical detail screen.
- `feat_device_lineage.png` was excluded because its companion capture contains
  a DataHub error; the successful `_v2` capture is used instead.

See [the explanatory evidence report](../../../../archive/legacy-docs/docs/edai1/DATAHUB_LINEAGE_ASSERTIONS_EVIDENCE.md).
