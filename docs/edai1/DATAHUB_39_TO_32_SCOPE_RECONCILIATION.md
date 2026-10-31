# DataHub 39-to-32 scope reconciliation

The architecture inventory contains 39 logical tables: Source 8 + Bronze 8 + Silver 8 + Gold 15. The physical DataHub publication deliberately contains 32 GCS datasets:

- eight source shards are represented by one immutable source-manifest dataset: `8 -> 1` (-7);
- Bronze remains 8 and Silver remains 8;
- Gold remains 15, split as 11 core Gold plus 4 feature tables.

Therefore `1 + 8 + 8 + 11 + 4 = 32`. This is a representation change, not a missing-table defect. The plan also publishes 31 exact dataset-to-dataset lineage aspects: 8 Source→Bronze, 8 Bronze→Silver, 11 curated→core Gold and 4 curated→feature outputs.

