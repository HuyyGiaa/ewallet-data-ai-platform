# Warehouse index benchmark

The reproducible local PostgreSQL benchmark used 2,000,000 rows and seven repetitions. The same selective query and data snapshot produced median latency 37.940 ms before and 10.414 ms after indexing (72.55% improvement) with equal results. Write overhead increased 122.25%, so the index is justified only for a read-heavy access pattern.

A second query was not added: it would exercise the same filter/index family without materially increasing confidence. This benchmark does not authorize changes to the MLflow metadata database or select it as the cloud analytics warehouse.

