# Runtime Configuration

## Trạng thái

Runtime configuration model đã được triển khai trong Task 1 của cloud refactor.
Các profile thật `local`, `dev` và `gcp` sẽ được thêm ở Task 2. Vì vậy tài liệu
này mô tả contract đã có trong code nhưng chưa tuyên bố cloud pipeline đã chạy.

## Mục đích

Trước refactor, đường dẫn MinIO, endpoint và Spark settings nằm rải rác trong
Spark I/O, validators, benchmark, Feast và metadata code. Runtime config tạo một
nguồn cấu hình chung để business transformations không cần biết chúng đang đọc
`s3a://` local hay `gs://` trên GCP.

Code chính:

- `data_platform/config/models.py`: immutable configuration models và URI rules;
- `data_platform/config/loader.py`: YAML loading, environment interpolation và
  schema validation;
- `data_platform/config/__init__.py`: public imports cho các consumer.

## Chọn environment

`load_runtime_config()` chọn environment theo thứ tự:

1. tham số `environment` truyền trực tiếp;
2. biến `EWALLET_ENV`;
3. mặc định `local`.

Chỉ ba tên `local`, `dev` và `gcp` hợp lệ. Loader đọc:

```text
configs/environments/<environment>/platform.yaml
```

Task 1 tests dùng temporary profiles để kiểm tra contract. Tracked profiles sẽ
được tạo trong Task 2.

## Environment placeholders

Hai dạng placeholder được hỗ trợ:

```yaml
required_value: ${EWALLET_DATA_BUCKET}
value_with_default: ${GCP_REGION:-asia-southeast1}
```

`${NAME}` fail ngay nếu biến thiếu hoặc rỗng. `${NAME:-default}` dùng default
khi biến thiếu hoặc rỗng. Interpolation áp dụng đệ quy cho mapping, list và
tuple nhưng không thay đổi object đầu vào.

Error chỉ nêu tên biến hoặc field bị lỗi. Loader không in toàn bộ environment,
rendered configuration hoặc credential value.

## Models

`RuntimeConfig` gồm:

| Model | Trách nhiệm |
|---|---|
| `StorageConfig` | source/Bronze/Silver/Gold/Feast roots và private connector options |
| `SparkConfig` | master, shuffle partitions, event log, packages và Spark properties |
| `ServiceConfig` | DataHub và optional Trino endpoints |
| `RuntimeConfig` | environment, GCP project/region và ba model trên |

Dataclasses là frozen. Mapping được copy sang read-only mapping để caller không
thể thay đổi config sau preflight. Storage options và Spark configurations bị
loại khỏi `repr` vì chúng có thể chứa credential hoặc connector secrets.

## Logical table thành physical URI

Consumer không tự ghép bucket/path. Nó gọi:

```python
config.storage.table_uri("gold", "fact_transactions")
```

Ví dụ với `gold_root: gs://bucket/lakehouse/gold/`, kết quả là:

```text
gs://bucket/lakehouse/gold/fact_transactions
```

Loader loại dấu `/` cuối root. `table_name` phải là một relative name duy nhất;
empty name, absolute path, slash và `..` bị reject để tránh đọc/ghi sai prefix.

## Failure modes cần biết

| Log/error | Ý nghĩa | Cách xử lý |
|---|---|---|
| `Missing required environment variable: NAME` | Profile cần một runtime value chưa được set | Set biến được nêu; không hardcode nó vào YAML |
| `unsupported environment` | `EWALLET_ENV` hoặc CLI value sai | Dùng `local`, `dev` hoặc `gcp` |
| `contains unexpected field(s)` | YAML có typo hoặc field ngoài contract | Sửa field theo model thay vì bỏ qua warning |
| `Missing required configuration field` | Profile chưa đầy đủ | Bổ sung field vào đúng section |
| `configuration.environment does not match` | Folder và nội dung profile không đồng nhất | Sửa `environment` trong YAML |

## Validation checkpoint

Task 1 được xác nhận bằng:

```bash
python -m unittest tests.unit.test_runtime_config -v
```

Kết quả operator-run ngày 2026-10-21: **13/13 tests PASS**. Task này không kết
nối MinIO, GCS, Spark, DataHub hoặc Trino.
