"""
Sinh 1 sự kiện transaction tại 1 thời điểm — logic nghiệp vụ giống hệt
generate_transactions() bên offline (insufficient-fund -> failed, merchant/channel
skew), chỉ khác đơn vị: mỗi lần gọi build_event() trả về đúng 1 dict, không phải
cả DataFrame.

QUAN TRỌNG: TYPE_WEIGHTS bên dưới PHẢI khớp với TYPE_WEIGHTS đang dùng trong
offline_generator.py. Tốt nhất nên chuyển TYPE_WEIGHTS sang fintech_schema.py
làm hằng số dùng chung, để tránh 2 nơi định nghĩa lệch nhau khi sau này chỉnh sửa.
Copy đúng giá trị bạn đang dùng ở offline vào đây trước khi chạy.
"""

import random
import uuid
from datetime import datetime, timedelta

from data_generator.src.fintech_schema import TransactionType, TransactionStatus, TYPE_WEIGHTS

from .state_loader import StreamingState

_TYPE_KEYS = list(TYPE_WEIGHTS.keys())
_TYPE_PROBS = list(TYPE_WEIGHTS.values())


def _pick_status_non_failed(cfg: dict) -> str:
    success_w = cfg.get("status_success_weight", 0.98)
    pending_w = cfg.get("status_pending_weight", 0.02)
    return random.choices(
        [TransactionStatus.SUCCESS.value, TransactionStatus.PENDING.value],
        weights=[success_w, pending_w],
    )[0]


def build_event(state: StreamingState, now: datetime) -> dict:
    account_id = random.choice(state.account_ids)
    user_id = state.account_to_user[account_id]
    tx_type = random.choices(_TYPE_KEYS, weights=_TYPE_PROBS)[0]

    old_balance = state.balances[account_id]
    amount = round(random.uniform(10_000, 2_000_000), 2)

    merchant_id = None
    counterparty_account_id = None

    if tx_type == TransactionType.DEPOSIT:
        new_balance = round(old_balance + amount, 2)
        tx_status = _pick_status_non_failed(state.cfg)
    else:
        if tx_type == TransactionType.PAYMENT:
            merchant_id = state.pick_merchant()
        elif tx_type == TransactionType.TRANSFER:
            other_accounts = [a for a in state.account_ids if a != account_id]
            counterparty_account_id = random.choice(other_accounts) if other_accounts else None

        if amount > old_balance:
            new_balance = old_balance
            tx_status = TransactionStatus.FAILED.value
        else:
            amount = round(amount, 2)
            new_balance = round(old_balance - amount, 2)
            tx_status = _pick_status_non_failed(state.cfg)

    # Cập nhật state — MUTABLE, ảnh hưởng tới lần build_event() kế tiếp cho account này
    state.balances[account_id] = new_balance

    user_devices = state.devices_by_user.get(user_id, [])
    device_id = random.choice(user_devices) if user_devices else None
    channel = state.pick_channel(user_id)

    late_rate = state.cfg.get("late_arrival_rate", 0.12)
    delay_min, delay_max = state.cfg.get("late_delay_min_max", [5, 1440])

    if random.random() < late_rate:
        delay_minutes = random.randint(delay_min, delay_max)
        event_time = now - timedelta(minutes=delay_minutes)
        ingested_at = now
    else:
        event_time = now
        ingested_at = now

    return {
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
        "timestamp": event_time.isoformat(),
        "ingested_at": ingested_at.isoformat(),
    }