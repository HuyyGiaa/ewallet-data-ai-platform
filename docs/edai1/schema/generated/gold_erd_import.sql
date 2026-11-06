-- ERD import only. Load into an empty scratch PostgreSQL database and reverse
-- engineer it in DBeaver. These constraints are logical; they do not exist in
-- the Delta tables, and this file holds no data.

CREATE SCHEMA IF NOT EXISTS gold;

CREATE SCHEMA IF NOT EXISTS features;

CREATE TABLE gold.dim_account (
    "account_id" TEXT,
    "user_id" TEXT,
    "account_type" TEXT,
    "currency" TEXT,
    "created_at" TIMESTAMPTZ,
    PRIMARY KEY ("account_id")
);

CREATE TABLE gold.dim_date (
    "date_key" INTEGER,
    "calendar_date" DATE,
    "day" INTEGER,
    "month" INTEGER,
    "quarter" INTEGER,
    "year" INTEGER,
    "day_of_week" TEXT,
    "is_weekend" BOOLEAN,
    PRIMARY KEY ("date_key")
);

CREATE TABLE gold.dim_device (
    "device_id" TEXT,
    "user_id" TEXT,
    "device_type" TEXT,
    "os" TEXT,
    "first_seen_at" TIMESTAMPTZ,
    PRIMARY KEY ("device_id")
);

CREATE TABLE gold.dim_merchant (
    "merchant_id" TEXT,
    "merchant_name" TEXT,
    "category" TEXT,
    PRIMARY KEY ("merchant_id")
);

CREATE TABLE gold.dim_user (
    "user_id" TEXT,
    "full_name" TEXT,
    "email" TEXT,
    "phone" TEXT,
    "kyc_verified" BOOLEAN,
    "created_at" TIMESTAMPTZ,
    PRIMARY KEY ("user_id")
);

CREATE TABLE gold.fact_balance_snapshot (
    "account_id" TEXT,
    "date_key" INTEGER,
    "snapshot_date" DATE,
    "closing_balance" DOUBLE PRECISION,
    PRIMARY KEY ("account_id", "snapshot_date")
);

CREATE TABLE gold.fact_login_events (
    "login_id" TEXT,
    "user_id" TEXT,
    "device_id" TEXT,
    "date_key" INTEGER,
    "is_success" BOOLEAN,
    "login_ts" TIMESTAMPTZ,
    "event_date" DATE,
    PRIMARY KEY ("login_id")
);

CREATE TABLE gold.fact_transactions (
    "transaction_id" TEXT,
    "user_id" TEXT,
    "account_id" TEXT,
    "merchant_id" TEXT,
    "device_id" TEXT,
    "counterparty_account_id" TEXT,
    "date_key" INTEGER,
    "type" TEXT,
    "status" TEXT,
    "channel" TEXT,
    "currency" TEXT,
    "amount" DOUBLE PRECISION,
    "old_balance" DOUBLE PRECISION,
    "new_balance" DOUBLE PRECISION,
    "timestamp" TIMESTAMPTZ,
    "ingested_at" TIMESTAMPTZ,
    "event_date" DATE,
    PRIMARY KEY ("transaction_id")
);

CREATE TABLE gold.feat_user_90d (
    "user_id" TEXT,
    "f_user_total_transactions_90d" BIGINT,
    "f_user_avg_transaction_amount_90d" DOUBLE PRECISION,
    "f_user_failed_transaction_rate_90d" DOUBLE PRECISION,
    "f_user_distinct_merchants_90d" BIGINT,
    "event_timestamp" TIMESTAMPTZ,
    "created_timestamp" TIMESTAMPTZ,
    PRIMARY KEY ("user_id")
);

CREATE TABLE gold.obt_transaction_enriched (
    "transaction_id" TEXT,
    "user_id" TEXT,
    "kyc_verified" BOOLEAN,
    "user_created_at" TIMESTAMPTZ,
    "account_id" TEXT,
    "account_type" TEXT,
    "account_currency" TEXT,
    "account_created_at" TIMESTAMPTZ,
    "device_id" TEXT,
    "device_type" TEXT,
    "os" TEXT,
    "first_seen_at" TIMESTAMPTZ,
    "merchant_id" TEXT,
    "merchant_name" TEXT,
    "merchant_category" TEXT,
    "date_key" INTEGER,
    "day_of_week" TEXT,
    "month" INTEGER,
    "quarter" INTEGER,
    "year" INTEGER,
    "is_weekend" BOOLEAN,
    "type" TEXT,
    "status" TEXT,
    "channel" TEXT,
    "currency" TEXT,
    "amount" DOUBLE PRECISION,
    "old_balance" DOUBLE PRECISION,
    "new_balance" DOUBLE PRECISION,
    "counterparty_account_id" TEXT,
    "timestamp" TIMESTAMPTZ,
    "ingested_at" TIMESTAMPTZ,
    "event_date" DATE,
    PRIMARY KEY ("transaction_id")
);

CREATE TABLE gold.opt_merchant_performance (
    "merchant_id" TEXT,
    "merchant_name" TEXT,
    "category" TEXT,
    "transaction_count" BIGINT,
    "total_amount" DOUBLE PRECISION,
    "avg_amount" DOUBLE PRECISION,
    "success_count" BIGINT,
    "failed_count" BIGINT,
    "distinct_users" BIGINT,
    PRIMARY KEY ("merchant_id")
);

CREATE TABLE features.feat_account_behavior (
    "account_id" TEXT,
    "event_timestamp" TIMESTAMPTZ,
    "account_tx_count_5m" BIGINT,
    "account_tx_count_1h" BIGINT,
    "account_tx_count_24h" BIGINT,
    "account_amount_sum_1h" DOUBLE PRECISION,
    "account_amount_sum_24h" DOUBLE PRECISION,
    "account_amount_observation_count_30d" BIGINT,
    "account_avg_amount_30d" DOUBLE PRECISION,
    "account_std_amount_30d" DOUBLE PRECISION,
    "account_failed_rate_24h" DOUBLE PRECISION,
    "account_seconds_since_last_tx" DOUBLE PRECISION,
    PRIMARY KEY ("account_id", "event_timestamp")
);

CREATE TABLE features.feat_device_behavior (
    "device_id" TEXT,
    "event_timestamp" TIMESTAMPTZ,
    "device_age_seconds" DOUBLE PRECISION,
    "device_tx_count_1h" BIGINT,
    "device_tx_count_24h" BIGINT,
    "device_amount_sum_24h" DOUBLE PRECISION,
    "device_failed_rate_24h" DOUBLE PRECISION,
    PRIMARY KEY ("device_id", "event_timestamp")
);

CREATE TABLE features.feat_merchant_behavior (
    "merchant_id" TEXT,
    "event_timestamp" TIMESTAMPTZ,
    "merchant_tx_count_10m" BIGINT,
    "merchant_tx_count_1h" BIGINT,
    "merchant_tx_count_24h" BIGINT,
    "merchant_unique_users_10m" BIGINT,
    "merchant_unique_users_1h" BIGINT,
    "merchant_amount_sum_1h" DOUBLE PRECISION,
    "merchant_avg_amount_24h" DOUBLE PRECISION,
    PRIMARY KEY ("merchant_id", "event_timestamp")
);

CREATE TABLE features.feat_user_behavior (
    "user_id" TEXT,
    "event_timestamp" TIMESTAMPTZ,
    "user_tx_count_5m" BIGINT,
    "user_tx_count_1h" BIGINT,
    "user_tx_count_24h" BIGINT,
    "user_amount_sum_1h" DOUBLE PRECISION,
    "user_amount_observation_count_30d" BIGINT,
    "user_avg_amount_30d" DOUBLE PRECISION,
    "user_std_amount_30d" DOUBLE PRECISION,
    "user_failed_rate_24h" DOUBLE PRECISION,
    "user_distinct_merchants_24h" BIGINT,
    PRIMARY KEY ("user_id", "event_timestamp")
);

ALTER TABLE gold.dim_account ADD FOREIGN KEY ("user_id") REFERENCES gold.dim_user ("user_id");
ALTER TABLE gold.dim_device ADD FOREIGN KEY ("user_id") REFERENCES gold.dim_user ("user_id");
ALTER TABLE gold.fact_transactions ADD FOREIGN KEY ("user_id") REFERENCES gold.dim_user ("user_id");
ALTER TABLE gold.fact_transactions ADD FOREIGN KEY ("account_id") REFERENCES gold.dim_account ("account_id");
ALTER TABLE gold.fact_transactions ADD FOREIGN KEY ("device_id") REFERENCES gold.dim_device ("device_id");
ALTER TABLE gold.fact_transactions ADD FOREIGN KEY ("merchant_id") REFERENCES gold.dim_merchant ("merchant_id");
ALTER TABLE gold.fact_transactions ADD FOREIGN KEY ("counterparty_account_id") REFERENCES gold.dim_account ("account_id");
ALTER TABLE gold.fact_transactions ADD FOREIGN KEY ("date_key") REFERENCES gold.dim_date ("date_key");
ALTER TABLE gold.fact_login_events ADD FOREIGN KEY ("user_id") REFERENCES gold.dim_user ("user_id");
ALTER TABLE gold.fact_login_events ADD FOREIGN KEY ("device_id") REFERENCES gold.dim_device ("device_id");
ALTER TABLE gold.fact_login_events ADD FOREIGN KEY ("date_key") REFERENCES gold.dim_date ("date_key");
ALTER TABLE gold.fact_balance_snapshot ADD FOREIGN KEY ("account_id") REFERENCES gold.dim_account ("account_id");
ALTER TABLE gold.fact_balance_snapshot ADD FOREIGN KEY ("date_key") REFERENCES gold.dim_date ("date_key");
ALTER TABLE gold.obt_transaction_enriched ADD FOREIGN KEY ("transaction_id") REFERENCES gold.fact_transactions ("transaction_id");
ALTER TABLE gold.feat_user_90d ADD FOREIGN KEY ("user_id") REFERENCES gold.dim_user ("user_id");
ALTER TABLE gold.opt_merchant_performance ADD FOREIGN KEY ("merchant_id") REFERENCES gold.dim_merchant ("merchant_id");
ALTER TABLE features.feat_user_behavior ADD FOREIGN KEY ("user_id") REFERENCES gold.dim_user ("user_id");
ALTER TABLE features.feat_account_behavior ADD FOREIGN KEY ("account_id") REFERENCES gold.dim_account ("account_id");
ALTER TABLE features.feat_device_behavior ADD FOREIGN KEY ("device_id") REFERENCES gold.dim_device ("device_id");
ALTER TABLE features.feat_merchant_behavior ADD FOREIGN KEY ("merchant_id") REFERENCES gold.dim_merchant ("merchant_id");
