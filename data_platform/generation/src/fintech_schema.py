"""
Định nghĩa schema cho 7 bảng trong domain Fintech / E-Wallet.
Khớp đúng với DDL đã tạo trong PostgreSQL (fintech_db).

Bảng: users, accounts, merchants, devices, transactions,
      balance_snapshots (composite PK), login_events
"""

from enum import Enum


class TransactionType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER = "transfer"
    PAYMENT = "payment"


class TransactionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


class Channel(str, Enum):
    APP = "app"
    WEB = "web"
    ATM = "atm"


class AccountType(str, Enum):
    WALLET_VND = "wallet_vnd"
    POINTS = "points"


class MerchantCategory(str, Enum):
    GROCERY = "grocery"
    ECOMMERCE = "ecommerce"
    UTILITY = "utility"
    TRANSPORT = "transport"
    FOOD_DELIVERY = "food_delivery"
    ENTERTAINMENT = "entertainment"


class DeviceType(str, Enum):
    MOBILE = "mobile"
    TABLET = "tablet"
    DESKTOP = "desktop"


TYPE_WEIGHTS = {
    TransactionType.DEPOSIT: 0.20,
    TransactionType.WITHDRAW: 0.15,
    TransactionType.TRANSFER: 0.25,
    TransactionType.PAYMENT: 0.40,
}

STATUS_WEIGHTS = {
    TransactionStatus.SUCCESS: 0.93,
    TransactionStatus.FAILED: 0.05,
    TransactionStatus.PENDING: 0.02,
}


def validate_balance(tx_type: str, old_balance: float, new_balance: float, amount: float) -> bool:
    """Kiểm tra business rule: new_balance khớp old_balance +- amount theo loại giao dịch."""
    if tx_type == TransactionType.DEPOSIT.value:
        return abs(new_balance - (old_balance + amount)) < 1e-6
    if tx_type in (TransactionType.WITHDRAW.value, TransactionType.TRANSFER.value, TransactionType.PAYMENT.value):
        return abs(new_balance - (old_balance - amount)) < 1e-6
    return True
