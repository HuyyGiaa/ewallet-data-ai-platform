from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

OFFLINE_DATA_DIR = (
    PROJECT_ROOT / "data_generator" / "output" / "offline"
)

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")

# Chạy local bằng HTTP, không dùng HTTPS.
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

BRONZE_BUCKET = "bronze-zone"
SILVER_BUCKET = "silver-zone"
GOLD_BUCKET = "gold-zone"
METASTORE_BUCKET = "lakehouse"

REQUIRED_BUCKETS = (
    BRONZE_BUCKET,
    SILVER_BUCKET,
    GOLD_BUCKET,
    METASTORE_BUCKET,
)

# Delta Lake configuration
DELTA_STORAGE_OPTIONS = {
    "endpoint_url": f"http://{MINIO_ENDPOINT}",
    "access_key_id": MINIO_ACCESS_KEY,
    "secret_access_key": MINIO_SECRET_KEY,
    "region": "us-east-1",
    "allow_http": "true",
    "allow_unsafe_rename": "true",
}

# Trino configuration
TRINO_HOST = os.getenv("TRINO_HOST", "localhost")
TRINO_PORT = int(os.getenv("TRINO_PORT", "8081"))
TRINO_USER = os.getenv("TRINO_USER", "trino")
TRINO_CATALOG = os.getenv("TRINO_CATALOG", "delta")

BRONZE_SCHEMA = "bronze_zone"
SILVER_SCHEMA = "silver_zone"
GOLD_SCHEMA = "gold_zone"

# Offline tables
OFFLINE_TABLES = (
    "users",
    "accounts",
    "merchants",
    "devices",
    "transactions",
    "balance_snapshots",
    "login_events",
)

# ============================================================
# Bronze table schema normalization
# ============================================================

TABLE_DATETIME_COLUMNS = {
    "users": ("created_at",),
    "accounts": ("created_at",),
    "merchants": (),
    "devices": ("first_seen_at",),
    "transactions": ("timestamp", "ingested_at"),
    "balance_snapshots": (),
    "login_events": ("login_ts",),
}

TABLE_DATE_COLUMNS = {
    "users": (),
    "accounts": (),
    "merchants": (),
    "devices": (),
    "transactions": (),
    "balance_snapshots": ("snapshot_date",),
    "login_events": (),
}

TABLE_STRING_COLUMNS = {
    "users": (
        "user_id",
        "full_name",
        "email",
        "phone",
    ),
    "accounts": (
        "account_id",
        "user_id",
        "account_type",
        "currency",
    ),
    "merchants": (
        "merchant_id",
        "merchant_name",
        "category",
    ),
    "devices": (
        "device_id",
        "user_id",
        "device_type",
        "os",
    ),
    "transactions": (
        "transaction_id",
        "account_id",
        "user_id",
        "device_id",
        "type",
        "currency",
        "status",
        "channel",
        "merchant_id",
        "counterparty_account_id",
    ),
    "balance_snapshots": (
        "account_id",
    ),
    "login_events": (
        "login_id",
        "user_id",
        "device_id",
    ),
}

def parquet_path(table_name: str) -> Path:
    """Trả về đường dẫn file Parquet nguồn của một bảng offline."""
    return OFFLINE_DATA_DIR / f"{table_name}.parquet"


def delta_table_uri(bucket: str, table_name: str) -> str:
    """Trả về URI của Delta table trên MinIO."""
    return f"s3://{bucket}/{table_name}"