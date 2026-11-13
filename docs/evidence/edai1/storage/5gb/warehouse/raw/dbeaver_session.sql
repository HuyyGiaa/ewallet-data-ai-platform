-- EDAI-1 warehouse benchmark: DBeaver session for WH01-WH06.
-- Connection: PostgreSQL, host 127.0.0.1, port 55432, database warehouse, user warehouse
-- (password = $EDAI1_WH_PG_PASSWORD). Run each block with Ctrl+Enter, in this order.
-- Data-derived parameters of this run: {'window_start': '2026-05-25', 'window_end': '2026-05-31', 'merchant_id': '00058dec-9436-4664-91e0-3e1d5829e22b', 'status': 'success', 'expected_rows': 2}

-- WH01  benchmark table, schema, row count and 5GB source provenance
SELECT key, value FROM benchmark_provenance ORDER BY key;
SELECT count(*) AS rows, min(event_date) AS first_day, max(event_date) AS last_day,
       count(DISTINCT merchant_id) AS merchants FROM fact_transactions;
SELECT column_name, data_type, is_nullable FROM information_schema.columns
WHERE table_name = 'fact_transactions' ORDER BY ordinal_position;

-- WH02  BEFORE indexing (only the primary key exists)
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'fact_transactions';
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS tx_count, round(sum(amount::numeric), 2) AS total_amount, round(avg(amount::numeric), 4) AS avg_amount FROM fact_transactions WHERE merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b' AND event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND status = 'success';

-- WH03  create the index, then show its actual definition and size
CREATE INDEX idx_fact_transactions_merchant_date ON fact_transactions (merchant_id, event_date) INCLUDE (status, amount);
VACUUM (ANALYZE) fact_transactions;
SELECT indexname, pg_get_indexdef(indexname::regclass) AS definition,
       pg_size_pretty(pg_relation_size(indexname::regclass)) AS size
FROM pg_indexes WHERE tablename = 'fact_transactions' ORDER BY indexname;

-- WH04  AFTER indexing (same query, same parameters)
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS tx_count, round(sum(amount::numeric), 2) AS total_amount, round(avg(amount::numeric), 4) AS avg_amount FROM fact_transactions WHERE merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b' AND event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND status = 'success';

-- WH05  before/after latency from the benchmark's measured runs (after the `after` phase)
SELECT experiment, side, count(*) AS runs,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY execution_ms)::numeric, 3) AS median_ms,
       round(min(execution_ms)::numeric, 3) AS min_ms, round(max(execution_ms)::numeric, 3) AS max_ms,
       min(access_path) AS access_path
FROM benchmark_runs WHERE experiment IN ('read', 'interleaved')
GROUP BY experiment, side ORDER BY experiment, side;

-- WH06  write overhead and semantic equivalence
SELECT side, count(*) AS batches,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY execution_ms)::numeric, 3) AS median_insert_ms,
       pg_size_pretty(sum(wal_bytes)) AS wal_written
FROM benchmark_runs WHERE experiment = 'write' GROUP BY side ORDER BY side;
SELECT (SELECT count(*) FROM ingest_without_index) AS rows_without_index,
       (SELECT count(*) FROM ingest_with_index) AS rows_with_index,
       (SELECT md5(string_agg(transaction_id, ',' ORDER BY transaction_id)) FROM ingest_without_index)
     = (SELECT md5(string_agg(transaction_id, ',' ORDER BY transaction_id)) FROM ingest_with_index) AS same_rows;
SELECT count(*) AS tx_count, round(sum(amount::numeric), 2) AS total_amount, round(avg(amount::numeric), 4) AS avg_amount FROM fact_transactions WHERE merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b' AND event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31' AND status = 'success';
SELECT pg_size_pretty(pg_relation_size('idx_fact_transactions_merchant_date')) AS index_size,
       pg_size_pretty(pg_relation_size('fact_transactions')) AS table_size;
