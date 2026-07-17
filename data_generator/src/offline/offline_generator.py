"""
Data Generator chính - sinh đầy đủ 7 bảng offline cho domain Fintech E-Wallet.

Chạy: python src/main.py --config config/settings.yaml

Output: users.parquet, accounts.parquet, merchants.parquet, devices.parquet,
        transactions.parquet, balance_snapshots.parquet, login_events.parquet
"""

import argparse
import os
import random
import uuid
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yaml
from faker import Faker
from pathlib import Path
from data_generator.src.fintech_schema import (
    TransactionType,
    TransactionStatus,
    Channel,
    AccountType,
    MerchantCategory,
    DeviceType,
    TYPE_WEIGHTS,
    STATUS_WEIGHTS,
)

fake = Faker("vi_VN")

PEAK_HOURS = [7, 8, 9, 12, 18, 19, 20]


def load_config(config_path="../config/settings.yaml"):
    BASE_DIR = Path(__file__).resolve().parent.parent
    full_path = BASE_DIR / config_path
    
    with open(full_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    Faker.seed(seed)


# 1. users
def generate_users(cfg: dict) -> pd.DataFrame:
    n = cfg["n_users"]
    end_date = datetime.now()
    start_date = end_date - timedelta(days=cfg["days_history"] + 365)

    rows = []
    for _ in range(n):
        rows.append({
            "user_id": str(uuid.uuid4()),
            "full_name": fake.name(),
            "email": fake.email(),
            "phone": fake.phone_number(),
            "kyc_verified": random.random() < 0.85,
            "created_at": fake.date_time_between(start_date=start_date, end_date=end_date - timedelta(days=30)),
        })
    return pd.DataFrame(rows)


# 2. accounts
def generate_accounts(users_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, u in users_df.iterrows():
        rows.append({
            "account_id": str(uuid.uuid4()),
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
            "merchant_id": str(uuid.uuid4()),
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
                "device_id": str(uuid.uuid4()),
                "user_id": u["user_id"],
                "device_type": dtype,
                "os": random.choice(os_by_type[dtype]),
                "first_seen_at": u["created_at"],
            })
    return pd.DataFrame(rows)


# 5. transactions 
def generate_transactions(accounts_df: pd.DataFrame, merchants_df: pd.DataFrame, devices_df: pd.DataFrame, cfg: dict,) -> pd.DataFrame:
    n = cfg.get("n_transactions") or len(accounts_df) * 8
    end_date = datetime.now()
    start_date = end_date - timedelta(days=cfg["days_history"])
    schema_change_date = datetime.fromisoformat(cfg["schema_change_date"])

    account_ids = accounts_df["account_id"].tolist()
    account_to_user = dict(zip(accounts_df["account_id"], accounts_df["user_id"]))
    merchant_ids = merchants_df["merchant_id"].tolist()

    devices_by_user = devices_df.groupby("user_id")["device_id"].apply(list).to_dict()

    balances = {acc_id: round(random.uniform(2_000_000, 30_000_000), 2) for acc_id in account_ids}


    skew_ratio_channel = cfg.get("skew_ratio_channel", 0.6)
    user_ids = list(account_to_user.values())
    user_channel_pref = {
        uid: ("app" if random.random() < skew_ratio_channel else None)
        for uid in set(user_ids)
    }
    LOYAL_CHANNEL_WEIGHT = 0.9
    OTHER_CHANNELS = [c.value for c in Channel if c.value != "app"]

    def pick_channel(user_id: str) -> str:
        pref = user_channel_pref.get(user_id)
        if pref == "app":
            if random.random() < LOYAL_CHANNEL_WEIGHT:
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
                other_accounts = [a for a in account_ids if a != account_id]
                counterparty_account_id = random.choice(other_accounts) if other_accounts else None
            
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
            "transaction_id": str(uuid.uuid4()),
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
    end_date = datetime.now()
    start_date = end_date - timedelta(days=cfg["days_history"])
    devices_by_user = devices_df.groupby("user_id")["device_id"].apply(list).to_dict()

    n_logins_per_user = 15  # trung bình mỗi user login ~15 lần trong khung thời gian
    rows = []
    for _, u in users_df.iterrows():
        user_devices = devices_by_user.get(u["user_id"], [])
        if not user_devices:
            continue
        for _ in range(random.randint(1, n_logins_per_user)):
            rows.append({
                "login_id": str(uuid.uuid4()),
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

# Main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="config/settings.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    set_seed(cfg["random_seed"])

    output_dir = cfg.get("output_dir", "output/offline")
    os.makedirs(output_dir, exist_ok=True)

    print(f" {cfg['n_users']} users...")
    users_df = generate_users(cfg)

    print("Generate accounts...")
    accounts_df = generate_accounts(users_df)

    print(f"Generate {cfg['n_merchants']} merchants...")
    merchants_df = generate_merchants(cfg)

    print("Generate devices...")
    devices_df = generate_devices(users_df, cfg)

    print("Generate transactions...")
    transactions_df = generate_transactions(accounts_df, merchants_df, devices_df, cfg)
    print(f"      -> {len(transactions_df)} dòng trước khi chèn lỗi")
    transactions_df = apply_skew(transactions_df, cfg)
    transactions_df = apply_duplicates(transactions_df, cfg)
    print(f"      -> {len(transactions_df)} dòng sau khi chèn skew + duplicate")

    print("Generate balance_snapshots từ transactions...")
    balance_snapshots_df = generate_balance_snapshots(transactions_df)

    print("Generate login_events...")
    login_events_df = generate_login_events(users_df, devices_df, cfg)

    BASE_DIR = Path(__file__).resolve().parent.parent
    out_dir = BASE_DIR / cfg.get("output_dir", "output/offline")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\nĐang xuất file ra thư mục: {out_dir}")
    
    users_df.to_parquet(out_dir / "users.parquet", index=False)
    accounts_df.to_parquet(out_dir / "accounts.parquet", index=False)
    merchants_df.to_parquet(out_dir / "merchants.parquet", index=False)
    devices_df.to_parquet(out_dir / "devices.parquet", index=False)
    transactions_df.to_parquet(out_dir / "transactions.parquet", index=False)
    balance_snapshots_df.to_parquet(out_dir / "balance_snapshots.parquet", index=False)
    login_events_df.to_parquet(out_dir / "login_events.parquet", index=False)

    print(f"\nHoàn tất. Output tại: {output_dir}/")
    print(f"  users: {len(users_df)} | accounts: {len(accounts_df)} | merchants: {len(merchants_df)}")
    print(f"  devices: {len(devices_df)} | transactions: {len(transactions_df)}")
    print(f"  balance_snapshots: {len(balance_snapshots_df)} | login_events: {len(login_events_df)}")


if __name__ == "__main__":
    main()
