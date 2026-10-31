# Đề xuất giao diện thay đổi schema cho Spark owner

Trạng thái: `OWNER_DEPENDENCY`, chưa triển khai runtime. Tài liệu này không sửa
DP2/DP3, không đổi Delta canonical, không đổi 4 FeatureViews hay Feast Registry.

## Row 35 — SCD Type 2

Áp dụng trước tiên cho `gold.dim_user`, grain `(user_id, valid_from_ts)`:

- `valid_from_ts TIMESTAMP NOT NULL`, inclusive;
- `valid_to_ts TIMESTAMP NULL`, exclusive; `NULL` cho bản ghi hiện hành;
- `is_current BOOLEAN NOT NULL`;
- đúng một bản ghi `is_current=true` cho mỗi `user_id`;
- các khoảng `[valid_from_ts, valid_to_ts)` không chồng lấp;
- thay đổi thuộc tính tạo version mới; chạy lại cùng input/run-id phải idempotent;
- fact lookup dùng `event_ts >= valid_from_ts AND event_ts < coalesce(valid_to_ts, +infinity)`.

Gate tích hợp: fixture có insert mới, thay đổi thuộc tính, late correction và rerun;
kiểm tra history bảo toàn, một current row, zero overlap, lookup đúng trước/sau mốc.

## Row 36 — thời điểm tạo feature

Các bảng `gold.feat_user_behavior`, `feat_account_behavior`,
`feat_device_behavior`, `feat_merchant_behavior` bổ sung:

- `created TIMESTAMP NOT NULL`: thời điểm artifact được ghi, không phải event time;
- `event_timestamp` giữ nguyên nghĩa PIT và grain hiện tại;
- `created >= event_timestamp` chỉ là metric quan sát, không phải invariant cho backfill;
- rerun phải ghi provenance `run_id`, nhưng không thay đổi 31 feature hay PIT window `[T-window,T)`.

Gate tích hợp: schema readback từ Delta, validation non-null, timestamps theo UTC,
row count/feature hashes không đổi ngoài cột mới, Feast parity do MLOps owner chạy lại.

## Trình tự bàn giao

1. Spark owner triển khai trong worktree Spark, không cherry-pick ngược tài liệu này.
2. Chạy unit/fixture tests và một bounded non-canonical run path.
3. Review schema diff + equivalence; chỉ sau phê duyệt mới tạo snapshot cloud mới.
4. MLOps owner xác nhận PIT parity trước khi thay đổi source contract của Feast.
