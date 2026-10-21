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

Silver/Gold pipeline, validators, Feast adapter và hai F7 training-source
exporter đều nhận cùng `RuntimeConfig`. Ví dụ `transactions` được đọc từ
`s3a://silver-zone/transactions` trong local profile và từ
`gs://<bucket>/lakehouse/silver/transactions` trong GCP profile mà không đổi
transformation hoặc training contract.

## Ranh giới local storage bootstrap

Các lệnh sau là công cụ tương thích cho Docker/MinIO/Trino trên laptop và chỉ
chấp nhận profile `local`:

```bash
python -m data_platform.storage.scripts.init_bucket --environment local
python -m data_platform.storage.scripts.init_storage \
  --environment local --include-fraud-labels
python -m data_platform.storage.scripts.register_trino_tables \
  --environment local --layer all --include-fraud-labels
```

Chúng kiểm tra environment trước khi tạo bucket, ghi Delta hoặc sửa metadata
Trino. Chọn `dev` hay `gcp` sẽ dừng với thông báo `local-only command` trước
mọi mutation. Endpoint Trino, endpoint MinIO và MinIO credentials được đọc từ
runtime profile đã render; module storage không còn tự gán password mặc định.

`source_root` của local bootstrap phải là URI `file://` chứa các file Parquet
nguồn. Có thể đặt riêng trên máy mà không sửa YAML tracked:

```bash
export EWALLET_SOURCE_ROOT="file://$(pwd)/data_platform/generation/output/offline"
```

GCS của `dev`/`gcp` sẽ được provision bằng Terraform trong bước hạ tầng cloud.
Không dùng các lệnh MinIO trên làm cách tạo bucket GCS thủ công, và Trino local
không phải dependency bắt buộc của cloud pipeline.

## Failure modes cần biết

| Log/error | Ý nghĩa | Cách xử lý |
|---|---|---|
| `Missing required environment variable: NAME` | Profile cần một runtime value chưa được set | Set biến được nêu; không hardcode nó vào YAML |
| `unsupported environment` | `EWALLET_ENV` hoặc CLI value sai | Dùng `local`, `dev` hoặc `gcp` |
| `contains unexpected field(s)` | YAML có typo hoặc field ngoài contract | Sửa field theo model thay vì bỏ qua warning |
| `Missing required configuration field` | Profile chưa đầy đủ | Bổ sung field vào đúng section |
| `configuration.environment does not match` | Folder và nội dung profile không đồng nhất | Sửa `environment` trong YAML |

## Foundation validation

Chạy cổng kiểm soát R1 từ repository root:

```bash
python ops/validation/validate_runtime_config.py
```

Lệnh này parse đủ ba profile bằng giá trị test, parse chín logical data
contracts và quét operational Python/YAML để chặn MinIO path, DataHub/Trino
localhost, credential mẫu hoặc đường dẫn máy cá nhân bị viết lại vào source.
Ngoại lệ chỉ được khai báo cho từng file local cụ thể; cloud profile không được
thừa hưởng allowlist của local.

Evidence máy đọc được nằm tại
[`r1_config_foundation.json`](../evidence/cloud/r1_config_foundation.json).
Nó ghi commit nền, số test và kết quả scan, không ghi credential, bucket thật
hoặc absolute developer path.

R1 chỉ hoàn tất ranh giới cấu hình. Local profile là runtime đã vận hành và các
số MinIO hiện có là historical evidence. Dev/GCP mới là tracked configuration;
Terraform provisioning là R2 kế tiếp. Spark cloud benchmark/optimization và
canonical 100 GB chưa được chạy.
