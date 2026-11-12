-- Run as user `viewer` in the schema-viz Trino. Read-only.

-- ===== fact_transactions (source gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/lakehouse/runs/ab-20261110225456-optimized-gold-r261110225458/gold/fact_transactions @ Delta v0) =====
-- ST01 identity: the clones' history shows the CLONE of the pinned source version
SELECT version, timestamp, operation, operation_parameters FROM delta.storage_bench."fact_transactions_baseline$history";
SELECT version, timestamp, operation, operation_parameters FROM delta.storage_bench."fact_transactions_compacted$history";
SELECT version, timestamp, operation, operation_parameters FROM delta.storage_bench."fact_transactions_optimized$history";
-- ST02 file layout of the baseline arm
SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, count(DISTINCT event_date) AS partitions FROM delta.storage_bench.fact_transactions_baseline;
-- ST04 file layout of the compacted arm
SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, count(DISTINCT event_date) AS partitions FROM delta.storage_bench.fact_transactions_compacted;
-- ST04 file layout of the optimized arm
SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, count(DISTINCT event_date) AS partitions FROM delta.storage_bench.fact_transactions_optimized;
-- ST05/ST07 date_entity_lookup on baseline (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_baseline WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b';
-- ST05/ST07 date_entity_lookup on compacted (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_compacted WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b';
-- ST05/ST07 date_entity_lookup on optimized (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_optimized WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b';
-- ST05/ST07 date_range on baseline (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_baseline WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST05/ST07 date_range on compacted (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_compacted WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST05/ST07 date_range on optimized (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_optimized WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST09 partition pruning: pruned
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_baseline WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST09 partition pruning: stats_only
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_baseline WHERE "timestamp" >= TIMESTAMP '2026-05-25 00:00:00' AND "timestamp" < TIMESTAMP '2026-06-01 00:00:00';
-- ST09 partition pruning: full_scan
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.fact_transactions_baseline WHERE format_datetime("timestamp", 'yyyy-MM-dd') BETWEEN '2026-05-25' AND '2026-05-31';

-- ===== obt_transaction_enriched (source gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/lakehouse/runs/ab-20261110225456-optimized-gold-r261110225458/gold/obt_transaction_enriched @ Delta v0) =====
-- ST01 identity: the clones' history shows the CLONE of the pinned source version
SELECT version, timestamp, operation, operation_parameters FROM delta.storage_bench."obt_transaction_enriched_baseline$history";
SELECT version, timestamp, operation, operation_parameters FROM delta.storage_bench."obt_transaction_enriched_compacted$history";
SELECT version, timestamp, operation, operation_parameters FROM delta.storage_bench."obt_transaction_enriched_optimized$history";
-- ST02 file layout of the baseline arm
SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, count(DISTINCT event_date) AS partitions FROM delta.storage_bench.obt_transaction_enriched_baseline;
-- ST04 file layout of the compacted arm
SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, count(DISTINCT event_date) AS partitions FROM delta.storage_bench.obt_transaction_enriched_compacted;
-- ST04 file layout of the optimized arm
SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, count(DISTINCT event_date) AS partitions FROM delta.storage_bench.obt_transaction_enriched_optimized;
-- ST05/ST07 date_entity_lookup on baseline (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_baseline WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b';
-- ST05/ST07 date_entity_lookup on compacted (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_compacted WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b';
-- ST05/ST07 date_entity_lookup on optimized (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_optimized WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b';
-- ST05/ST07 date_range on baseline (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_baseline WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST05/ST07 date_range on compacted (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_compacted WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST05/ST07 date_range on optimized (merchant 00058dec-9436-4664-91e0-3e1d5829e22b)
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_optimized WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST09 partition pruning: pruned
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_baseline WHERE event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31';
-- ST09 partition pruning: stats_only
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_baseline WHERE "timestamp" >= TIMESTAMP '2026-05-25 00:00:00' AND "timestamp" < TIMESTAMP '2026-06-01 00:00:00';
-- ST09 partition pruning: full_scan
EXPLAIN ANALYZE SELECT COUNT(*) AS row_count, SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount FROM delta.storage_bench.obt_transaction_enriched_baseline WHERE format_datetime("timestamp", 'yyyy-MM-dd') BETWEEN '2026-05-25' AND '2026-05-31';
