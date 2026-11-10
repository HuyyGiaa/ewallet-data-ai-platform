"""Rebuild the pre-Phase-B feature golden fixture (run manually, not by the tests).

1. Inputs: a small deterministic dataset from the real generator
   (``settings.yaml``, seed 20261110). Timestamps are stored as UTC epoch
   microseconds so the fixture is independent of any time zone.
2. Baseline: the feature builders of the pre-Phase-B commit, extracted with
   ``git show <base>:<path>`` into a temporary package. The current checkout's
   builders are not used, so the golden output is a genuine pre-Phase-B result.
3. Golden output: every builder output, timestamps as epoch microseconds, sorted
   by key; ``created_timestamp`` (write time) is dropped.
4. ``PROVENANCE.json``: base commit, generator settings, versions and the
   SHA-256 of every fixture file, which the test checks before comparing.

Usage, from the repository root with PySpark available:

    python tests/fixtures/feature_equivalence/build_golden.py --base f871b33a
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SEED = 20261110
SIZES = {"n_users": 300, "n_merchants": 30, "n_transactions": 3000}
BUILDER_FILES = (
    "features/__init__.py",
    "features/account_features.py",
    "features/device_features.py",
    "features/merchant_features.py",
    "features/temporal.py",
    "features/user_features.py",
    "feature_builder.py",
)
TRANSACTION_COLUMNS = ("user_id", "account_id", "device_id", "merchant_id", "timestamp", "amount", "status", "type")


def micros(series: pd.Series) -> pd.Series:
    return (pd.to_datetime(series).dt.tz_localize("UTC").astype("datetime64[us, UTC]").astype("int64"))


def build_inputs() -> dict[str, pd.DataFrame]:
    sys.path.insert(0, str(ROOT))
    from data_platform.generation.src.offline.offline_generator import generate_offline_datasets

    config = yaml.safe_load((ROOT / "data_platform/generation/config/settings.yaml").read_text(encoding="utf-8"))
    config = copy.deepcopy(config)
    config.update(SIZES, random_seed=SEED)
    config["fraud"]["random_seed"] = SEED
    data, _ = generate_offline_datasets(config)
    tx = pd.concat([data["transactions_v1"], data["transactions_v2"]], ignore_index=True)
    # Silver keeps one row per transaction_id; the fixture models Silver input.
    tx = tx.drop_duplicates("transaction_id").sort_values("transaction_id").reset_index(drop=True)
    transactions = tx[list(TRANSACTION_COLUMNS)].copy()
    transactions["timestamp_us"] = micros(transactions.pop("timestamp"))
    devices = data["devices"][["device_id", "user_id"]].copy()
    devices["first_seen_at_us"] = micros(data["devices"]["first_seen_at"])
    users = data["users"][["user_id"]].copy()
    return {"transactions": transactions, "devices": devices.sort_values("device_id"),
            "users": users.sort_values("user_id")}


def extract_base_builders(base: str, target: Path) -> str:
    package = target / "base_features"
    (package / "features").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for relative in BUILDER_FILES:
        source = subprocess.run(
            ["git", "show", f"{base}:data_platform/processing/spark/gold/{relative}"],
            cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout
        (package / relative).write_text(source, encoding="utf-8")
    sys.path.insert(0, str(target))
    return subprocess.run(["git", "rev-parse", base], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.strip()


def spark_inputs(spark, inputs: dict[str, pd.DataFrame]):
    from pyspark.sql import functions as F

    transactions = spark.createDataFrame(inputs["transactions"]).withColumn(
        "timestamp", F.expr("timestamp_micros(timestamp_us)")).drop("timestamp_us")
    devices = spark.createDataFrame(inputs["devices"]).withColumn(
        "first_seen_at", F.expr("timestamp_micros(first_seen_at_us)")).drop("first_seen_at_us")
    return transactions, devices, spark.createDataFrame(inputs["users"])


def to_golden(frame, key: tuple[str, ...]) -> pd.DataFrame:
    """Timestamps as epoch microseconds, write-time columns dropped, sorted by key."""
    from pyspark.sql import functions as F
    from pyspark.sql.types import TimestampType

    frame = frame.drop(*[c for c in ("created", "created_timestamp") if c in frame.columns])
    columns = [F.unix_micros(F.col(f.name)).alias(f"{f.name}_us") if isinstance(f.dataType, TimestampType)
               else F.col(f.name) for f in frame.schema.fields]
    return frame.select(*columns).toPandas().sort_values(list(key)).reset_index(drop=True)


GOLDEN_KEYS = {
    "feat_user_behavior": ("user_id", "event_timestamp_us"),
    "feat_account_behavior": ("account_id", "event_timestamp_us"),
    "feat_device_behavior": ("device_id", "event_timestamp_us"),
    "feat_merchant_behavior": ("merchant_id", "event_timestamp_us"),
    "feat_user_90d": ("user_id",),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="f871b33a")
    args = parser.parse_args()
    from pyspark.sql import SparkSession

    inputs = build_inputs()
    with tempfile.TemporaryDirectory() as directory:
        base_sha = extract_base_builders(args.base, Path(directory))
        features = importlib.import_module("base_features.features")
        feature_builder = importlib.import_module("base_features.feature_builder")
        spark = (SparkSession.builder.master("local[2]").config("spark.ui.enabled", "false")
                 .config("spark.sql.session.timeZone", "UTC").config("spark.sql.shuffle.partitions", "4")
                 .getOrCreate())
        transactions, devices, users = spark_inputs(spark, inputs)
        outputs = {
            "feat_user_behavior": features.build_feat_user_behavior(transactions),
            "feat_account_behavior": features.build_feat_account_behavior(transactions),
            "feat_device_behavior": features.build_feat_device_behavior(transactions, devices),
            "feat_merchant_behavior": features.build_feat_merchant_behavior(transactions),
            "feat_user_90d": feature_builder.build_feat_user_90d(transactions, users),
        }
        golden = {name: to_golden(frame, GOLDEN_KEYS[name]) for name, frame in outputs.items()}
        spark_version = spark.version
        spark.stop()
    files = {}
    for name, frame in {**{f"input_{k}": v for k, v in inputs.items()}, **golden}.items():
        path = HERE / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        files[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": len(frame)}
    provenance = {
        "purpose": "Pre-Phase-B feature outputs for the permanent equivalence regression.",
        "base_commit": base_sha,
        "builders": [f"data_platform/processing/spark/gold/{item}" for item in BUILDER_FILES],
        "generator": {"config": "data_platform/generation/config/settings.yaml", "random_seed": SEED, **SIZES},
        "spark_version": spark_version,
        "pandas_version": pd.__version__,
        "excluded_columns": ["created_timestamp (write time)"],
        "timestamps": "UTC epoch microseconds (*_us columns)",
        "files": files,
    }
    (HERE / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(provenance["files"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
