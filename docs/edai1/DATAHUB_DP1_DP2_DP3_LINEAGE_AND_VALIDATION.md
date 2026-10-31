# DataHub DP1/DP2/DP3 lineage and validation

The publisher creates three Airflow DataFlows and four DataJobs:

| Pipeline | Job | Inputs | Outputs |
|---|---|---:|---:|
| DP1 | `submit_bronze_dataproc` | 1 manifest | 8 Bronze |
| DP2 | `submit_silver_dataproc` | 8 Bronze | 8 Silver |
| DP2 | `submit_core_gold_dataproc` | 8 Silver | 11 core Gold |
| DP3 | `submit_historical_features_dataproc` | 19 curated | 4 feature tables |

Live local GMS publication verified 42/42 aspects by immediate typed readback: 3 `dataFlowInfo`, 4 `dataJobInfo`, 4 `dataJobInputOutput`, and 31 `upstreamLineage`. A second idempotent run produced the same evidence SHA-256 (`cb28be6e7f7abf579d8129be2d1fa80986585ce5a96e6a71acf70628dc7d99f2`). Genuine UI screenshots show the Airflow catalogue and DP1 lineage graph.

Validation is intentionally separated from lineage. Historical validator logs prove the exact table-set gates, but no new DataHub `assertionInfo`/`assertionRunEvent` aspects were published in this campaign. Rubric rows 29, 31 and 33 therefore remain unclaimed until result-bound assertions with run ID, timestamp and exact dataset URN are published and read back.

