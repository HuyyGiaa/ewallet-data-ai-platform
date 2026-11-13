-- V1 inventory per zone (expected source_zone 10, bronze_zone 9, silver_zone 9, gold_zone 16)
SELECT table_schema, count(*) AS tables FROM (SELECT table_schema, table_name FROM delta.information_schema.tables UNION ALL SELECT table_schema, table_name FROM hive.information_schema.tables) WHERE table_schema <> 'information_schema' GROUP BY table_schema ORDER BY table_schema;
-- V2 total (expected 44)
SELECT count(*) AS total_tables FROM (SELECT table_name FROM delta.information_schema.tables WHERE table_schema <> 'information_schema' UNION ALL SELECT table_name FROM hive.information_schema.tables WHERE table_schema <> 'information_schema');
-- V3 the four previously missing tables are registered
SHOW TABLES FROM hive.source_zone LIKE 'merchant_history';
SHOW TABLES FROM delta.bronze_zone LIKE 'merchant_history';
SHOW TABLES FROM delta.silver_zone LIKE 'merchant_history';
SHOW TABLES FROM delta.gold_zone LIKE 'dim_merchant_scd2';
-- V4 locations: see 10_provenance_registrar.txt (SHOW CREATE TABLE needs registrar) and 11_metastore_locations_5gb.tsv
-- V5 readability of the SCD2 input and dimension
SELECT count(*) AS rows, count(DISTINCT merchant_id) AS merchants FROM delta.silver_zone.merchant_history;
SELECT count(*) AS versions, count(DISTINCT merchant_id) AS merchants, count_if(is_current) AS current_rows FROM delta.gold_zone.dim_merchant_scd2;
SELECT merchant_sk, merchant_id, valid_from_ts, valid_to_ts, is_current FROM delta.gold_zone.dim_merchant_scd2 ORDER BY merchant_id, valid_from_ts LIMIT 3;
-- V6 representative existing tables are readable, one per zone
SELECT count(*) AS rows FROM hive.source_zone.accounts;
SELECT count(*) AS rows FROM delta.bronze_zone.transactions;
SELECT count(*) AS rows FROM delta.silver_zone.transactions;
SELECT count(*) AS rows FROM delta.gold_zone.fact_transactions;
SELECT count(*) AS rows FROM delta.gold_zone.feat_user_behavior;
