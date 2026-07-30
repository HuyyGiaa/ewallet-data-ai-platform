-- Bảng nguồn OLTP giả lập, đóng vai trò "database của app ví điện tử".
-- Debezium sẽ CDC trực tiếp trên bảng này.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id UUID PRIMARY KEY,
    account_id UUID NOT NULL,
    user_id UUID NOT NULL,
    type VARCHAR(20) NOT NULL,
    amount NUMERIC(18, 2) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'VND',
    status VARCHAR(20) NOT NULL,
    channel VARCHAR(20) NOT NULL,
    old_balance NUMERIC(18, 2) NOT NULL,
    new_balance NUMERIC(18, 2) NOT NULL,
    merchant_id UUID,
    counterparty_account_id UUID,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- Bật logical replication để Debezium đọc được WAL (chỉ cần chạy 1 lần)
ALTER TABLE transactions REPLICA IDENTITY FULL;