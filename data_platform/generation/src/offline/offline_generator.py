"""Offline generator for the Fintech E-Wallet domain.

Transactions được xuất thành 2 physical batches để mô phỏng
schema evolution. Fraud ground truth is exported separately and never changes
the transaction schemas.

Output:
- users.parquet
- accounts.parquet
- merchants.parquet
- devices.parquet
- transactions_v1.parquet
- transactions_v2.parquet
- balance_snapshots.parquet
- login_events.parquet
- fraud_labels.parquet
"""

import argparse
import os
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from faker import Faker
from data_platform.generation.src.fintech_schema import (
    TransactionType,
    TransactionStatus,
    Channel,
    AccountType,
    MerchantCategory,
    DeviceType,
    TYPE_WEIGHTS,
    STATUS_WEIGHTS,
)
from data_platform.generation.src.offline.fraud import inject_fraud

fake = Faker("vi_VN")
_ID_RANDOM = random.Random()

PEAK_HOURS = [7, 8, 9, 12, 18, 19, 20]


@dataclass(frozen=True)
class TemporalConfig:
    start: datetime
    cutover: datetime
    end: datetime


def get_temporal_config(cfg: dict) -> TemporalConfig:
    """Read and validate the deterministic offline event-time boundaries."""
    try:
        start = datetime.fromisoformat(cfg["generation"]["temporal"]["start_timestamp"])
        end = datetime.fromisoformat(cfg["generation"]["temporal"]["end_timestamp"])
        schema_cfg = cfg["schema_evolution"]
        cutover = datetime.fromisoformat(schema_cfg["cutover_timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "offline temporal configuration requires valid ISO-8601 generation start/end "
            "and schema_evolution.cutover_timestamp"
        ) from exc

    if schema_cfg.get("enabled") is not True:
        raise ValueError("schema_evolution.enabled must be true for V1/V2 generation")
    if not start < cutover < end:
        raise ValueError(
            "offline temporal invariant failed: start_timestamp < cutover_timestamp "
            "< end_timestamp is required"
        )
    return TemporalConfig(start=start, cutover=cutover, end=end)

def load_config(config_path=None):
    data_generator_dir = Path(__file__).resolve().parents[2]

    if config_path is None:
        full_path = data_generator_dir / "config" / "settings.yaml"
    else:
        full_path = Path(config_path)

        if not full_path.is_absolute():
            full_path = data_generator_dir / full_path

    full_path = full_path.resolve()

    print(f"[CONFIG] Loading: {full_path}")

    with open(full_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    Faker.seed(seed)
    _ID_RANDOM.seed(seed)


def deterministic_uuid() -> str:
    """Generate UUID-shaped identifiers from the reproducible base RNG."""
    return str(uuid.UUID(int=_ID_RANDOM.getrandbits(128), version=4))


# 1. users
def generate_users(cfg: dict) -> pd.DataFrame:
    n = cfg["n_users"]
    temporal = get_temporal_config(cfg)
    start_date = temporal.start - timedelta(days=365)
    end_date = temporal.start - timedelta(days=30)

    rows = []
    for _ in range(n):
        rows.append({
            "user_id": deterministic_uuid(),
            "full_name": fake.name(),
            "email": fake.email(),
            "phone": fake.phone_number(),
            "kyc_verified": random.random() < 0.85,
            "created_at": fake.date_time_between(start_date=start_date, end_date=end_date),
        })
    return pd.DataFrame(rows)


# 2. accounts
def generate_accounts(users_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, u in users_df.iterrows():
        rows.append({
            "account_id": deterministic_uuid(),
            "user_id": u["user_id"],
            "account_type": AccountType.WALLET_VND.value,
            "currency": "VND",
            "created_at": u["created_at"],
        })
    return pd.DataFrame(rows)


# 3. merchants
def generate_merchants(cfg: dict) -> pd.DataFrame:
    n = cfg["n_merchants"]
    categories = [c.value for c in MerchantCategory]
    rows = []
    for _ in range(n):
        rows.append({
            "merchant_id": deterministic_uuid(),
            "merchant_name": fake.company(),
            "category": random.choice(categories),
        })
    return pd.DataFrame(rows)


# 4. devices
def generate_devices(users_df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    device_types = [d.value for d in DeviceType]
    os_by_type = {
        "mobile": ["Android", "iOS"],
        "tablet": ["Android", "iPadOS"],
        "desktop": ["Windows", "macOS", "Linux"],
    }
    rows = []
    for _, u in users_df.iterrows():
        for _ in range(cfg.get("n_devices_per_user", 1)):
            dtype = random.choice(device_types)
            rows.append({
                "device_id": deterministic_uuid(),
                "user_id": u["user_id"],
                "device_type": dtype,
                "os": random.choice(os_by_type[dtype]),
                "first_seen_at": u["created_at"],
            })
    return pd.DataFrame(rows)


# 5. transactions 
def generate_transactions(accounts_df: pd.DataFrame, merchants_df: pd.DataFrame, devices_df: pd.DataFrame, cfg: dict,) -> pd.DataFrame:
    n = cfg.get("n_transactions") or len(accounts_df) * 8
    temporal = get_temporal_config(cfg)
    start_date = temporal.start
    end_date = temporal.end
    schema_change_date = temporal.cutover

    account_ids = accounts_df["account_id"].tolist()
    account_to_user = dict(zip(accounts_df["account_id"], accounts_df["user_id"]))
    merchant_ids = merchants_df["merchant_id"].tolist()

    devices_by_user = devices_df.groupby("user_id")["device_id"].apply(list).to_dict()

    balances = {acc_id: round(random.uniform(2_000_000, 30_000_000), 2) for acc_id in account_ids}


    skew_ratio_channel = cfg.get("skew_ratio_channel", 0.6)
    user_ids = list(account_to_user.values())
    user_channel_pref = {
        uid: ("app" if random.random() < skew_ratio_channel else None)
        for uid in sorted(set(user_ids))
    }
    loyal_channel_weight = cfg.get("channel_loyal_weight", 0.9)
    OTHER_CHANNELS = [c.value for c in Channel if c.value != "app"]

    def pick_channel(user_id: str) -> str:
        pref = user_channel_pref.get(user_id)
        if pref == "app":
            if random.random() < loyal_channel_weight:
                return "app"
            return random.choice(OTHER_CHANNELS)
        return random.choice([c.value for c in Channel])

    rows = []
    type_keys = list(TYPE_WEIGHTS.keys())
    type_probs = list(TYPE_WEIGHTS.values())
    status_keys = list(STATUS_WEIGHTS.keys())
    status_probs = list(STATUS_WEIGHTS.values())

    n_top = max(1, int(len(merchant_ids) * cfg.get("merchant_skew_top_pct", 0.05)))
    top_merchants = merchant_ids[:n_top]
    other_merchants = merchant_ids[n_top:]
    merchant_skew_traffic_pct = cfg.get("merchant_skew_traffic_pct", 0.80)

    def pick_merchant() -> str:
        if random.random() < merchant_skew_traffic_pct and top_merchants:
            return random.choice(top_merchants)
        return random.choice(other_merchants) if other_merchants else random.choice(merchant_ids)
    
    for _ in range(n):
        account_id = random.choice(account_ids)
        user_id = account_to_user[account_id]
        tx_type = random.choices(type_keys, weights=type_probs)[0]

        old_balance = balances[account_id]
        amount = round(random.uniform(10_000, 2_000_000), 2)
        
        merchant_id = None
        counterparty_account_id = None

        if tx_type == TransactionType.DEPOSIT:
            new_balance = round(old_balance + amount, 2)
            tx_status = random.choices(
                [TransactionStatus.SUCCESS.value, TransactionStatus.PENDING.value],
                weights = [0.98, 0.02]
            )[0]
        else:
            if tx_type == TransactionType.PAYMENT:
                merchant_id = pick_merchant()
                
            elif tx_type == TransactionType.TRANSFER:
                if len(account_ids) > 1:
                    counterparty_account_id = random.choice(account_ids)
                    while counterparty_account_id == account_id:
                        counterparty_account_id = random.choice(account_ids)
            
            if amount > old_balance:
                new_balance = old_balance
                tx_status = TransactionStatus.FAILED.value
            else:
                amount = round(amount, 2)
                new_balance = round(old_balance - amount, 2)
                tx_status = random.choices(
                    [TransactionStatus.SUCCESS.value, TransactionStatus.PENDING.value],
                    weights=[0.98, 0.02],
                )[0]

        balances[account_id] = new_balance

        event_time = fake.date_time_between(start_date=start_date, end_date=end_date)

        user_devices = devices_by_user.get(user_id, [])
        device_id = random.choice(user_devices) if user_devices else None

        channel = pick_channel(user_id) if event_time >= schema_change_date else None

        rows.append({
            "transaction_id": deterministic_uuid(),
            "account_id": account_id,
            "user_id": user_id,
            "device_id": device_id,
            "type": tx_type.value,
            "amount": amount,
            "currency": "VND",
            "status": tx_status,
            "channel": channel,
            "old_balance": old_balance,
            "new_balance": new_balance,
            "merchant_id": merchant_id,
            "counterparty_account_id": counterparty_account_id,
            "timestamp": event_time,
            "ingested_at": event_time,
        })

    return pd.DataFrame(rows)


# 6. balance_snapshots (derived từ transactions, composite key)
def generate_balance_snapshots(transactions_df: pd.DataFrame) -> pd.DataFrame:
    df = transactions_df.copy()
    df["snapshot_date"] = pd.to_datetime(df["timestamp"]).dt.date

    # với mỗi (account_id, snapshot_date), lấy new_balance của giao dịch cuối cùng trong ngày
    df_sorted = df.sort_values("timestamp")
    snapshots = (
        df_sorted.groupby(["account_id", "snapshot_date"])
        .agg(closing_balance=("new_balance", "last"))
        .reset_index()
    )
    return snapshots


# 7. login_events
def generate_login_events(users_df: pd.DataFrame, devices_df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    temporal = get_temporal_config(cfg)
    end_date = temporal.end
    start_date = temporal.start
    devices_by_user = devices_df.groupby("user_id")["device_id"].apply(list).to_dict()

    n_logins_per_user = 15  # trung bình mỗi user login ~15 lần trong khung thời gian
    rows = []
    for _, u in users_df.iterrows():
        user_devices = devices_by_user.get(u["user_id"], [])
        if not user_devices:
            continue
        for _ in range(random.randint(1, n_logins_per_user)):
            rows.append({
                "login_id": deterministic_uuid(),
                "user_id": u["user_id"],
                "device_id": random.choice(user_devices),
                "login_ts": fake.date_time_between(start_date=start_date, end_date=end_date),
                "is_success": random.random() < 0.95,  # 5% login thất bại (đúng nghiệp vụ thật)
            })
    return pd.DataFrame(rows)


# Error injection (offline problems - áp dụng riêng cho transactions)
def apply_skew(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ratio = cfg.get("skew_ratio_hour", 0.75)
    n_to_skew = int(len(df) * ratio)
    idx = df.sample(n=n_to_skew, random_state=cfg["random_seed"]).index

    def shift_to_peak(ts):
        peak_hour = random.choice(PEAK_HOURS)
        return ts.replace(hour=peak_hour, minute=random.randint(0, 59))

    df.loc[idx, "timestamp"] = df.loc[idx, "timestamp"].apply(shift_to_peak)
    df.loc[idx, "ingested_at"] = df.loc[idx, "timestamp"]
    return df


def apply_duplicates(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ratio = cfg.get("duplicate_rate_offline", 0.02)
    n_dup = int(len(df) * ratio)
    dup_rows = df.sample(n=n_dup, random_state=7).copy()
    dup_rows["ingested_at"] = dup_rows["ingested_at"] + pd.to_timedelta(
        np.random.randint(1, 30, size=len(dup_rows)), unit="s"
    )
    return pd.concat([df, dup_rows], ignore_index=True)

def split_transaction_schema_versions(df: pd.DataFrame, cfg: dict,):
    schema_change_date = pd.Timestamp(get_temporal_config(cfg).cutover)

    v1 = (
        df[df["timestamp"] < schema_change_date]
        .drop(columns=["channel"])
        .copy()
    )

    v2 = (
        df[df["timestamp"] >= schema_change_date]
        .copy()
    )

    if v1.empty or v2.empty:
        raise ValueError(
            "Schema evolution requires transactions "
            "both before and after schema_evolution.cutover_timestamp."
        )

    return v1, v2

def generate_offline_datasets(cfg: dict) -> tuple[dict[str, pd.DataFrame], dict]:
    """Generate all offline dataframes without writing them to disk."""
    temporal = get_temporal_config(cfg)
    set_seed(cfg["random_seed"])
    print(f"{cfg['n_users']} users...")
    users_df = generate_users(cfg)
    print("Generate accounts...")
    accounts_df = generate_accounts(users_df)
    print(f"Generate {cfg['n_merchants']} merchants...")
    merchants_df = generate_merchants(cfg)
    print("Generate devices...")
    devices_df = generate_devices(users_df, cfg)
    print("Generate transactions...")
    transactions_df = generate_transactions(
        accounts_df,
        merchants_df,
        devices_df,
        cfg,
    )

    print(f"      -> {len(transactions_df)} logical rows before issue injection")
    transactions_df = apply_skew(transactions_df, cfg)

    # Fraud runs after Phase 1 skew but before duplicates. At this point every
    # transaction_id is still unique, so labels keep logical-transaction grain.
    login_devices_df = devices_df
    transactions_df, devices_df, fraud_labels_df, fraud_metrics = inject_fraud(
        transactions_df,
        devices_df,
        merchants_df,
        cfg,
        temporal.start,
        temporal.cutover,
        temporal.end,
    )
    transactions_df = apply_duplicates(transactions_df, cfg)
    print(
        f"      -> {len(transactions_df)} physical rows after skew + fraud + duplicate"
    )
    print("Generate balance_snapshots from transactions...")
    balance_snapshots_df = generate_balance_snapshots(transactions_df)
    print("Generate login_events...")
    # Preserve Phase 1 login generation. Fraud-only recent devices remain valid
    # entity rows but are not retroactively inserted into historical logins.
    login_events_df = generate_login_events(users_df, login_devices_df, cfg)
    transactions_v1_df, transactions_v2_df = split_transaction_schema_versions(
        transactions_df,
        cfg,
    )
    print(
        f"Schema V1: {len(transactions_v1_df):,} rows | "
        f"channel={'channel' in transactions_v1_df.columns}"
    )

    print(
        f"Schema V2: {len(transactions_v2_df):,} rows | "
        f"channel={'channel' in transactions_v2_df.columns}"
    )

    return {
        "users": users_df,
        "accounts": accounts_df,
        "merchants": merchants_df,
        "devices": devices_df,
        "transactions_v1": transactions_v1_df,
        "transactions_v2": transactions_v2_df,
        "balance_snapshots": balance_snapshots_df,
        "login_events": login_events_df,
        "fraud_labels": fraud_labels_df,
    }, fraud_metrics


def write_offline_datasets(datasets: dict[str, pd.DataFrame], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, dataframe in datasets.items():
        dataframe.to_parquet(out_dir / f"{name}.parquet", index=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    data_generator_dir = Path(__file__).resolve().parents[2]
    out_dir = data_generator_dir / cfg.get("output_dir", "output/offline")
    datasets, fraud_metrics = generate_offline_datasets(cfg)

    print(f"\nWriting 9 physical outputs to: {out_dir}")
    write_offline_datasets(datasets, out_dir)
    for name, dataframe in datasets.items():
        print(f"  {name}: {len(dataframe):,}")
    print(f"  fraud scenario counts: {fraud_metrics['scenario_counts']}")
    print(f"\nComplete. Output at: {out_dir}")


if __name__ == "__main__":
    main()
