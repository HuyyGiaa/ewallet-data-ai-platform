# Runtime Configuration

## Trạng thái

Runtime configuration model và ba tracked profile `local`, `dev`, `gcp` đã được
triển khai. Các profile mới chỉ xác định runtime contract; chúng không tuyên bố
cloud resources đã được provision hoặc pipeline GCS đã chạy.

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

Tracked profiles nằm tại:

```text
configs/environments/
├── local/platform.yaml
├── dev/platform.yaml
└── gcp/platform.yaml
```

`local` giữ MinIO như một adapter phát triển. `dev` dùng prefix GCS riêng dưới
`gs://<bucket>/dev/`; `gcp` dùng canonical prefixes không có `dev/`.

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

Copy `.env.example` thành `.env` cho local development rồi thay các placeholder
bằng giá trị chỉ tồn tại trên máy của bạn. `.env` bị Git ignore; không copy ADC
JSON, access token hoặc credential thật vào tracked YAML.

## Preflight

Chạy kiểm tra trước khi tạo Spark session hoặc submit job:

```bash
python -m data_platform.config.cli --environment local --check
python -m data_platform.config.cli --environment dev --check
python -m data_platform.config.cli --environment gcp --check
```

Local cần `MINIO_ACCESS_KEY` và `MINIO_SECRET_KEY`. Dev/GCP cần
`GCP_PROJECT_ID`, `EWALLET_DATA_BUCKET` và `DATAHUB_GMS_URL`; region mặc định là
`asia-southeast1` nhưng có thể override bằng `GCP_REGION`.

Output thành công kết thúc bằng:

```text
CONFIG CHECK PASS
```

CLI chỉ hiển thị scheme, project/region và URI đã che bucket/authority. Nó không
in storage options, MinIO credentials hoặc toàn bộ environment.

## Spark runtime

`data_platform.processing.spark.common.spark_session.spark_options()` chuyển
`RuntimeConfig` thành Spark properties mà không import hoặc khởi động PySpark.
`create_spark_session()` chỉ đặt `.master()` khi profile khai báo master, nên:

- `local` chạy `local[4]` và nhận S3A connector từ local profile;
- `dev`/`gcp` không ép local master và không mang MinIO/S3A credential;
- shuffle partitions và event-log URI đến từ profile đang chọn;
- `optimized=True/False` chỉ thay AQE và skew-join switches, không thay input,
  storage connector hoặc business logic.

Package dependency được truyền riêng cho Delta session builder. Danh sách rỗng
không tạo `spark.jars.packages` rỗng hoặc malformed. Log khởi động chỉ ghi
environment, storage scheme và các option hiệu năng an toàn; không ghi storage
options hoặc credential.

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

Profile và CLI contract được kiểm tra bằng:

```bash
python -m unittest tests.contract.test_environment_profiles -v
```
