# Fintech E-Wallet Data Generator — Design Doc

## 1. Domain Overview

Dự án mô phỏng một nền tảng ví điện tử (e-wallet) quy mô vừa. Generator sinh ra:

- Dữ liệu lịch sử/tham chiếu (offline, Parquet)
- Sự kiện giao dịch thời gian thực (streaming, JSON qua Kafka)

Mục tiêu: hỗ trợ ingestion, transformation, và feature engineering ở các tầng phía sau,
đồng thời cố tình chèn các vấn đề chất lượng dữ liệu và xử lý thực tế (skew, duplicate,
late arrival, burst, schema evolution).

---

## 2. Offline Dataset Design

### 2.1 Offline Tables

| Table | Grain | Key Columns |
|-------|-------|-------------|
| users | one per user | user_id, full_name, email, phone, kyc_verified, created_at |
| accounts | one per account | account_id, user_id, account_type, currency, created_at |
| merchants | one per merchant | merchant_id, merchant_name, category |
| devices | one per device | device_id, user_id, device_type, os, first_seen_at |
| transactions | one per transaction | transaction_id, account_id, user_id,device_id, type, amount, status, channel, old_balance, new_balance, merchant_id, counterparty_account_id, timestamp |
| balance_snapshots | one per account per day | account_id, snapshot_date, closing_balance |
| login_events | one per login attempt | login_id, user_id, device_id, login_ts, is_success |

### 2.2 Offline Data Problems

**Compulsory:**
- **Skew (Time)**: 75% giao dịch tập trung vào các khung giờ cao điểm (7-9h, 12h, 18-20h)
- **Skew (Channel)**: 60% user chỉ dùng 1 loại channel (app), số còn lại phân bổ đều app/web/atm
- **Skew (Merchant)**: 80% giao dịch payment đổ vào 5% merchant top đầu (giả lập Shopee, Grab, điện/nước, Apple Store); 20% volume còn lại chia cho 95% merchant nhỏ
- **High cardinality**: transaction_id, login_id gần như unique; account_id, user_id lặp lại nhiều lần
- **Schema evolution**: field `channel` chỉ xuất hiện từ mốc `schema_change_date` (2026-05-01) trở đi 

**Optional chosen:**
- **Duplicate rate**: 2% duplicate trong `transactions`
- **Extreme high cardinality demo**: bộ dữ liệu riêng quy mô lớn (~2-5 triệu dòng) để benchmark `COUNT(DISTINCT device_id/counterparty_account_id)` vs `approx_count_distinct()` — chứng minh giá trị HLL bằng số liệu thời gian chạy thực tế, không chỉ lý thuyết

**Output:** Parquet, partition theo `transaction_date` (rút từ `timestamp`).

---

## 3. Streaming Dataset Design

### 3.1 Event Stream Schema

Một Kafka topic thống nhất (`transactions.raw`) với field `type` phân biệt loại giao dịch.

Key columns:
- `transaction_id`, `type` (deposit | withdraw | transfer | payment)
- `timestamp` (event time — thời điểm giao dịch thực xảy ra), `ingested_at` (row creation time — thời điểm hệ thống nhận được record)
- `account_id`, `user_id`, `device_id`, `channel` (app | web | atm)
- `merchant_id` (nullable, chỉ có khi type = payment), `counterparty_account_id` (nullable, chỉ có khi type = transfer)
- `amount`, `currency`, `status` (success | failed | pending)

### 3.2 Streaming Data Problems

**Compulsory:**
- **Bursts**: baseline ~50 event/phút → tăng lên ~1500 event/phút trong các cửa sổ 20 phút mô phỏng giờ khuyến mãi (VD: `12:00-12:20`, `20:00-20:20`).
- **Late arrivals**: 12% event có `ingested_at` trễ hơn `timestamp` (độ trễ từ 5 phút đến 1 ngày, phân bố lệch về phía trễ ngắn).

**Optional chosen:**
- **Duplicate events**: 1.5% duplicate (cùng transaction_id, xuất hiện lại sau 1-3 phút — mô phỏng retry ở producer).

**Output:** JSON qua Kafka topic `transactions.raw`.

---

## 4. Feature Engineering

Tính từ dữ liệu giao dịch và login của user, phục vụ Feature Store (Feast) ở Consumption Layer:

**Offline (ổn định, window 90 ngày):**
- `f_user_total_transactions_90d` — tổng số giao dịch
- `f_user_avg_transaction_amount_90d` — giá trị giao dịch trung bình
- `f_user_failed_transaction_rate_90d` — tỷ lệ giao dịch thất bại
- `f_user_distinct_merchants_90d` — số merchant khác nhau đã giao dịch

**Streaming (rolling window):**
- `f_stream_transaction_count_5m` — số giao dịch trong 5 phút gần nhất theo account_id
- `f_stream_total_amount_5m` — tổng giá trị giao dịch 5 phút gần nhất
- `f_stream_burst_activity_flag` — cờ đánh dấu đang trong giai đoạn traffic bất thường

Merge offline + streaming thành 1 feature table theo `user_id`, refresh mỗi 15 phút.

---

## 5. Generator Configuration

```yaml
n_users: 50000
n_accounts_per_user: 1
n_merchants: 500
n_devices_per_user: 1
days_history: 90
skew_ratio_hour: 0.75
skew_ratio_channel: 0.60
duplicate_rate_offline: 0.02
schema_change_date: "2026-05-01"
base_events_per_min: 50
burst_multiplier: 30
burst_windows: ["12:00-12:20", "20:00-20:20"]
late_arrival_rate: 0.12
late_delay_min_max: [5, 1440]
duplicate_rate_stream: 0.015
random_seed: 42
```

---

## 6. Deliverables

1. **Generator code** với tham số cấu hình được (xem `data_generator/*.py`).
2. **Data outputs**: Parquet (offline), JSON qua Kafka (streaming).
3. **Quality report**:
   - Phân bố skew (theo giờ / channel)
   - Cardinality: approx_count_distinct theo từng key column
   - Schema evolution: tỷ lệ null của `channel` ở các partition trước `schema_change_date`
   - Duplicate rate trước/sau dedup
   - Streaming: tỷ lệ burst/late/duplicate thực tế đo được so với cấu hình
4. **Write-up**: giải thích các lựa chọn optional problem và thiết kế feature.

---

## 7. Implementation Tips

- Dùng seed cố định (`random_seed: 42`) để đảm bảo tái tạo được kết quả.
- Định nghĩa dedup key rõ ràng: `transaction_id` (offline), `transaction_id + ingested_at` (streaming — vì duplicate stream có `ingested_at` khác nhau).
- Giữ business rule cơ bản khi sinh transaction (`new_balance = old_balance ± amount`) để dữ liệu "make sense" về mặt tài chính — xem `Transaction.validate_balance()` trong `schemas.py`.
- Sinh balance_snapshots bằng cách lấy `new_balance` cuối cùng trong ngày của mỗi account, không sinh độc lập để tránh mâu thuẫn số liệu.
