"""Shared storage inventories and runtime-derived location helpers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from data_platform.config import RuntimeConfig, StorageConfig


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEGACY_OFFLINE_DATA_DIR = (
    PROJECT_ROOT / "data_platform" / "generation" / "output" / "offline"
)

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

TRINO_USER = "trino"
TRINO_CATALOG = "delta"
BRONZE_SCHEMA = "bronze_zone"
SILVER_SCHEMA = "silver_zone"
GOLD_SCHEMA = "gold_zone"

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
    if include_fraud_labels:
        return OFFLINE_TABLES + (FRAUD_LABEL_TABLE,)
    return OFFLINE_TABLES


def silver_tables(include_fraud_labels: bool = False) -> tuple[str, ...]:
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

# Kept for import compatibility while callers migrate to
# trino_registration_layers(runtime_config=...). It contains inventory only.
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


def require_local_runtime(config: RuntimeConfig, command_name: str) -> None:
    """Reject local storage mutation commands before they touch infrastructure."""
    if config.environment != "local":
        raise RuntimeError(
            f"{command_name} is a local-only command; "
            f"selected environment={config.environment!r}"
        )


def _trino_root(uri: str) -> str:
    parsed = urlsplit(uri.rstrip("/"))
    scheme = "s3" if parsed.scheme == "s3a" else parsed.scheme
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def storage_table_uri(
    storage: StorageConfig,
    layer: str,
    table_name: str,
    *,
    delta_rs: bool = False,
) -> str:
    uri = storage.table_uri(layer, table_name)
    if delta_rs:
        return _trino_root(uri)
    return uri


def registration_table_uri(
    layer_config: dict[str, object],
    table_name: str,
) -> str:
    if "root" in layer_config:
        return f"{str(layer_config['root']).rstrip('/')}/{table_name}"
    return delta_table_uri(str(layer_config["bucket"]), table_name)


def trino_registration_layers(
    include_fraud_labels: bool = False,
    *,
    runtime_config: RuntimeConfig | None = None,
) -> dict[str, dict[str, object]]:
    tables = {
        "bronze": offline_tables(include_fraud_labels),
        "silver": silver_tables(include_fraud_labels),
        "gold": GOLD_TABLES,
    }
    schemas = {
        "bronze": BRONZE_SCHEMA,
        "silver": SILVER_SCHEMA,
        "gold": GOLD_SCHEMA,
    }
    if runtime_config is None:
        buckets = {
            "bronze": BRONZE_BUCKET,
            "silver": SILVER_BUCKET,
            "gold": GOLD_BUCKET,
        }
        return {
            layer: {
                "schema": schemas[layer],
                "bucket": buckets[layer],
                "tables": tables[layer],
            }
            for layer in ("bronze", "silver", "gold")
        }

    roots = {
        "bronze": runtime_config.storage.bronze_root,
        "silver": runtime_config.storage.silver_root,
        "gold": runtime_config.storage.gold_root,
    }
    return {
        layer: {
            "schema": schemas[layer],
            "root": _trino_root(roots[layer]),
            "tables": tables[layer],
        }
        for layer in ("bronze", "silver", "gold")
    }


def local_bucket_names(storage: StorageConfig) -> tuple[str, ...]:
    roots = (storage.bronze_root, storage.silver_root, storage.gold_root)
    buckets = tuple(dict.fromkeys(urlsplit(root).netloc for root in roots))
    if not all(buckets):
        raise ValueError("local S3A storage roots must include bucket names")
    return buckets + tuple(
        bucket for bucket in (METASTORE_BUCKET,) if bucket not in buckets
    )


def deltalake_storage_options(config: RuntimeConfig) -> dict[str, str]:
    options = config.storage.options
    endpoint = options.get("spark.hadoop.fs.s3a.endpoint")
    access_key = options.get("spark.hadoop.fs.s3a.access.key")
    secret_key = options.get("spark.hadoop.fs.s3a.secret.key")
    if not endpoint or not access_key or not secret_key:
        raise ValueError("local MinIO endpoint and credentials are required by profile")
    return {
        "endpoint_url": endpoint,
        "access_key_id": access_key,
        "secret_access_key": secret_key,
        "region": "us-east-1",
        "allow_http": (
            "true" if str(endpoint).lower().startswith("http://") else "false"
        ),
        "allow_unsafe_rename": "true",
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
    }


def parquet_path(
    table_name: str,
    storage: StorageConfig | None = None,
) -> Path:
    if storage is None:
        return LEGACY_OFFLINE_DATA_DIR / f"{table_name}.parquet"
    parsed = urlsplit(storage.source_root)
    if parsed.scheme != "file":
        raise ValueError("local bootstrap source_root must use file://")
    return Path(parsed.path) / f"{table_name}.parquet"


def delta_table_uri(bucket: str, table_name: str) -> str:
    """Backward-compatible local MinIO URI helper."""
    return f"s3://{bucket}/{table_name}"


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
