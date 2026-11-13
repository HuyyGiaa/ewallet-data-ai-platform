-- Read-only evidence queries for EDAI-1 Schema Design rows 35, 36 and 38.
-- Run as user `viewer` against the schema-viz Trino, after ../generated/trino_register_tables.sql.
-- They read the registered 5GB tables (dim_merchant_scd2 is about 78k rows; the feature queries
-- read one or two columns). No statement writes anything.
--   infra/docker/schema-viz/run_sql.sh viewer data_platform/schema/sql/schema_evidence_queries.sql
-- Each query mirrors a rule of data_platform/quality/scd2_quality.py or the DP3 feature validator,
-- so its result can be compared with the rule result in the campaign validation reports.

-- SC06.1 versions and merchants (expected: versions = 78294 as in comparison.json; merchants = 55800)
SELECT count(*) AS versions, count(DISTINCT merchant_id) AS merchants,
       sum(CASE WHEN is_current THEN 1 ELSE 0 END) AS current_versions
FROM delta.gold_zone.dim_merchant_scd2;

-- SC06.2 distribution of versions per merchant
SELECT versions_per_merchant, count(*) AS merchants
FROM (SELECT merchant_id, count(*) AS versions_per_merchant
      FROM delta.gold_zone.dim_merchant_scd2 GROUP BY merchant_id)
GROUP BY versions_per_merchant ORDER BY versions_per_merchant;

-- SC06.3 every version of the merchant with the most versions (lowest merchant_id on a tie)
SELECT merchant_id, merchant_sk, merchant_name, category, valid_from_ts, valid_to_ts, is_current
FROM delta.gold_zone.dim_merchant_scd2
WHERE merchant_id = (SELECT merchant_id FROM delta.gold_zone.dim_merchant_scd2
                     GROUP BY merchant_id ORDER BY count(*) DESC, merchant_id LIMIT 1)
ORDER BY valid_from_ts;

-- SC06.4 one current version per merchant (expected: 0) -- rule one_current_version_per_merchant
SELECT count(*) AS merchants_without_exactly_one_current
FROM (SELECT merchant_id, sum(CASE WHEN is_current THEN 1 ELSE 0 END) AS current_versions
      FROM delta.gold_zone.dim_merchant_scd2 GROUP BY merchant_id)
WHERE current_versions <> 1;

-- SC06.5 contiguous, non-overlapping intervals (expected: 0) -- rule contiguous_non_overlapping_intervals
SELECT count(*) AS interval_violations
FROM (SELECT valid_to_ts,
             lead(valid_from_ts) OVER (PARTITION BY merchant_id ORDER BY valid_from_ts) AS next_from
      FROM delta.gold_zone.dim_merchant_scd2)
WHERE (next_from IS NOT NULL AND (valid_to_ts IS NULL OR valid_to_ts <> next_from))
   OR (next_from IS NULL AND valid_to_ts IS NOT NULL);

-- SC06.6 is_current agrees with an open end; valid_to after valid_from (expected: 0, 0)
SELECT sum(CASE WHEN is_current <> (valid_to_ts IS NULL) THEN 1 ELSE 0 END) AS flag_mismatches,
       sum(CASE WHEN valid_to_ts IS NOT NULL AND valid_to_ts <= valid_from_ts THEN 1 ELSE 0 END) AS inverted_intervals
FROM delta.gold_zone.dim_merchant_scd2;

-- SC06.7 current versions equal dim_merchant (expected: 0) -- rule current_version_equals_dim_merchant
SELECT count(*) AS current_attribute_mismatches
FROM delta.gold_zone.dim_merchant d
FULL OUTER JOIN (SELECT * FROM delta.gold_zone.dim_merchant_scd2 WHERE is_current) v
  ON d.merchant_id = v.merchant_id
WHERE d.merchant_id IS NULL OR v.merchant_id IS NULL
   OR NOT (d.merchant_name IS NOT DISTINCT FROM v.merchant_name)
   OR NOT (d.category IS NOT DISTINCT FROM v.category);

-- SC06.8 as-of lookup: transactions of one merchant resolved to the version valid at their time
SELECT f.transaction_id, f.timestamp, v.merchant_sk, v.valid_from_ts, v.valid_to_ts, v.category
FROM delta.gold_zone.fact_transactions f
JOIN delta.gold_zone.dim_merchant_scd2 v
  ON f.merchant_id = v.merchant_id
 AND v.valid_from_ts <= f.timestamp AND (v.valid_to_ts IS NULL OR f.timestamp < v.valid_to_ts)
WHERE f.merchant_id = (SELECT merchant_id FROM delta.gold_zone.dim_merchant_scd2
                       GROUP BY merchant_id ORDER BY count(*) DESC, merchant_id LIMIT 1)
ORDER BY f.timestamp
LIMIT 20;

-- SC07.1 event_timestamp and created per feature table (expected: no NULLs, exactly 1 created value)
SELECT 'feat_user_behavior' AS table_name, count(*) AS rows_, count(event_timestamp) AS event_ts_non_null,
       count(created) AS created_non_null, count(DISTINCT created) AS created_values,
       min(event_timestamp) AS min_event_ts, max(event_timestamp) AS max_event_ts, min(created) AS created
FROM delta.gold_zone.feat_user_behavior
UNION ALL
SELECT 'feat_account_behavior', count(*), count(event_timestamp), count(created), count(DISTINCT created),
       min(event_timestamp), max(event_timestamp), min(created)
FROM delta.gold_zone.feat_account_behavior
UNION ALL
SELECT 'feat_device_behavior', count(*), count(event_timestamp), count(created), count(DISTINCT created),
       min(event_timestamp), max(event_timestamp), min(created)
FROM delta.gold_zone.feat_device_behavior
UNION ALL
SELECT 'feat_merchant_behavior', count(*), count(event_timestamp), count(created), count(DISTINCT created),
       min(event_timestamp), max(event_timestamp), min(created)
FROM delta.gold_zone.feat_merchant_behavior
UNION ALL
SELECT 'feat_user_90d', count(*), count(event_timestamp), count(created), count(DISTINCT created),
       min(event_timestamp), max(event_timestamp), min(created)
FROM delta.gold_zone.feat_user_90d
ORDER BY table_name;

-- SC07.2 legacy created_timestamp of feat_user_90d equals created (expected: 0)
SELECT count(*) AS created_mismatches
FROM delta.gold_zone.feat_user_90d
WHERE NOT (created IS NOT DISTINCT FROM created_timestamp);

-- SC07.3 the four DP3 tables share one created value (expected: 1)
SELECT count(DISTINCT created) AS distinct_created_across_dp3
FROM (SELECT created FROM delta.gold_zone.feat_user_behavior
      UNION ALL SELECT created FROM delta.gold_zone.feat_account_behavior
      UNION ALL SELECT created FROM delta.gold_zone.feat_device_behavior
      UNION ALL SELECT created FROM delta.gold_zone.feat_merchant_behavior);

-- SC07.4 point-in-time sample: five rows of one user, event time against materialization time
SELECT user_id, event_timestamp, created, user_tx_count_1h, user_tx_count_24h
FROM delta.gold_zone.feat_user_behavior
WHERE user_id = (SELECT min(user_id) FROM delta.gold_zone.feat_user_behavior)
ORDER BY event_timestamp
LIMIT 5;
