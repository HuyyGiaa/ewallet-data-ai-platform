from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

OFFLINE_DATA_DIR = (
    PROJECT_ROOT / "data_platform" / "generation" / "output" / "offline"
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
    "AWS_S3_ALLOW_UNSAFE_RENAME": "true"
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

SILVER_TABLES = OFFLINE_TABLES

FRAUD_LABEL_TABLE = "fraud_labels"


def offline_tables(include_fraud_labels: bool = False) -> tuple[str, ...]:
    """Return the transitional Bronze inventory for the selected mode."""
    if include_fraud_labels:
        return OFFLINE_TABLES + (FRAUD_LABEL_TABLE,)
    return OFFLINE_TABLES


def silver_tables(include_fraud_labels: bool = False) -> tuple[str, ...]:
    """Return the transitional Silver inventory for the selected mode."""
    if include_fraud_labels:
        return SILVER_TABLES + (FRAUD_LABEL_TABLE,)
    return SILVER_TABLES

GOLD_TABLES = (
    "dim_user",
    "dim_account",
    "dim_merchant",
    "dim_device",
    "dim_date",
    "fact_transactions",
    "fact_login_events",
    "fact_balance_snapshot",
    "obt_transaction_enriched",
    "feat_user_90d",
    "feat_user_behavior",
    "feat_account_behavior",
    "feat_device_behavior",
    "feat_merchant_behavior",
    "opt_merchant_performance",
)

TRINO_REGISTRATION_LAYERS = {
    "bronze": {
        "schema": BRONZE_SCHEMA,
        "bucket": BRONZE_BUCKET,
        "tables": OFFLINE_TABLES,
    },
    "silver": {
        "schema": SILVER_SCHEMA,
        "bucket": SILVER_BUCKET,
        "tables": SILVER_TABLES,
    },
    "gold": {
        "schema": GOLD_SCHEMA,
        "bucket": GOLD_BUCKET,
        "tables": GOLD_TABLES,
    },
}


def trino_registration_layers(
    include_fraud_labels: bool = False,
) -> dict[str, dict[str, object]]:
    """Build a registration inventory without mutating legacy constants."""
    return {
        "bronze": {
            "schema": BRONZE_SCHEMA,
            "bucket": BRONZE_BUCKET,
            "tables": offline_tables(include_fraud_labels),
        },
        "silver": {
            "schema": SILVER_SCHEMA,
            "bucket": SILVER_BUCKET,
            "tables": silver_tables(include_fraud_labels),
        },
        "gold": {
            "schema": GOLD_SCHEMA,
            "bucket": GOLD_BUCKET,
            "tables": GOLD_TABLES,
        },
    }

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
    "fraud_labels": (),
}

TABLE_DATE_COLUMNS = {
    "users": (),
    "accounts": (),
    "merchants": (),
    "devices": (),
    "transactions": (),
    "balance_snapshots": ("snapshot_date",),
    "login_events": (),
    "fraud_labels": (),
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
    "fraud_labels": (
        "transaction_id",
        "fraud_type",
    ),
}

def parquet_path(table_name: str) -> Path:
    """Trả về đường dẫn file Parquet nguồn của một bảng offline."""
    return OFFLINE_DATA_DIR / f"{table_name}.parquet"


def delta_table_uri(bucket: str, table_name: str) -> str:
    """Trả về URI của Delta table trên MinIO."""
    return f"s3://{bucket}/{table_name}"
