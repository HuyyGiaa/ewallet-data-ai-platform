# All-zones schema

| Zone | Canonical inventory | Naming / role |
|---|---:|---|
| Source | 8 logical source tables, one immutable manifest dataset in DataHub | generator output, never overwritten |
| Bronze | 8 | raw, source-shaped Delta |
| Silver | 8 | `stg_` cleansed/conformed Delta |
| Gold core | 11 | `dim_`, `fact_`, `obt_` analytical contracts |
| Gold feature | 4 | `feat_` historical feature contracts |

Total logical inventory: 39. The automated DataHub representation contains 32 physical datasets as reconciled separately. Existing relationship and naming proofs remain valid. SCD2 (`valid_from_ts`, `valid_to_ts`, `is_current`) and feature `created` changes are interface proposals only; their implementation belongs to the excluded Spark owner and must preserve frozen 4 FeatureViews / 31 features / 41 columns.

