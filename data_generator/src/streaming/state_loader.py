"""
Đọc lại dữ liệu offline đã sinh (users, accounts, merchants, devices, balance_snapshots)
để streaming generator nối tiếp đúng, không tự sinh entity mới -> đảm bảo account_id,
user_id, merchant_id trong sự kiện streaming luôn trỏ tới entity có thật.

Balance khởi đầu cho mỗi account = closing_balance mới nhất trong balance_snapshots
(nối tiếp liền mạch với lịch sử offline, không random lại từ đầu).
"""



import pandas as pd
from pathlib import Path
from dataclasses import dataclass
from data_generator.src.fintech_schema import Channel
import random

@dataclass
class StreamingState:
    account_ids: list
    account_to_user: dict
    merchant_ids: list
    top_merchants: list
    other_merchants: list
    cfg: dict
    balances: dict
    devices_by_user: dict
    user_channel_pref: dict 
    
    def pick_channel(self, user_id) -> str:
        pref = self.user_channel_pref.get(user_id)
        loyal_weight = self.cfg.get("channel_loyal_weight", 0.9)
        other_channels = [c for c in Channel if c != "app"]
        if pref == "app":
            if random.random() < loyal_weight:
                return "app"
        return random.choice(other_channels) 
            
    
    def pick_merchant(self) -> str:
        traffic_pct = self.cfg.get("merchant_skew_traffic_pct", 0.8)
        if random.random() < traffic_pct and self.top_merchants:
            return random.choice(self.top_merchants)
        return random.choice(self.other_merchants)
    
def resolve_dir_offline(data_offline: str):
    BASE_DIR = Path(__file__).resolve().parents[2]
    return BASE_DIR / "output" / "offline" / data_offline

def load_state(cfg: dict) -> StreamingState:
    users_df = pd.read_parquet(resolve_dir_offline("users.parquet"))
    accounts_df = pd.read_parquet(resolve_dir_offline("accounts.parquet"))
    merchants_df = pd.read_parquet(resolve_dir_offline("merchants.parquet"))
    devices_df = pd.read_parquet(resolve_dir_offline("devices.parquet"))
    balance_snap_df = pd.read_parquet(resolve_dir_offline("balance_snapshots.parquet"))
    
    account_ids = accounts_df["account_id"].to_list()
    merchant_ids = merchants_df["merchant_id"].to_list()
    account_to_user = dict(zip(accounts_df["account_id"], accounts_df["user_id"]))
    devices_by_user = devices_df.groupby("user_id")["device_id"].apply(list).to_dict()
    
    latest_snap = (
        balance_snap_df
        .sort_values("snapshot_date")
        .groupby("account_id")
        .tail(1)
        .set_index("account_id")["closing_balance"]
    )
    
    balances = {}
    for acc_id in account_ids:
        balances[acc_id] = float(latest_snap.get(acc_id, 0.0))
        
        # --- Merchant skew: cố định 1 lần, đọc lại đúng tham số đã dùng ở offline ---
    n_top = max(1, int(len(merchant_ids) * cfg.get("merchant_skew_top_pct", 0.05)))
    top_merchants = merchant_ids[:n_top]
    other_merchants = merchant_ids[n_top:]

    # --- Channel preference theo user: tính lại (không lưu từ offline, chấp nhận
    #     random mới độc lập cho streaming — đủ dùng vì mục tiêu là mô phỏng thói quen,
    #     không bắt buộc user phải giữ đúng y hệt preference đã dùng ở offline) ---
    skew_ratio_channel = cfg.get("skew_ratio_channel", 0.6)
    user_channel_pref = {
        uid: ("app" if random.random() < skew_ratio_channel else None)
        for uid in set(account_to_user.values())
    }

    return StreamingState(
        account_ids=account_ids,
        account_to_user=account_to_user,
        merchant_ids=merchant_ids,
        top_merchants=top_merchants,
        other_merchants=other_merchants,
        devices_by_user=devices_by_user,
        balances=balances,
        user_channel_pref=user_channel_pref,
        cfg=cfg,
    )