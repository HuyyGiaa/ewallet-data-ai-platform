# Cloud Refactor Foundation Implementation Plan

> **Historical plan/spec — not current source of truth.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tạo nền tảng cấu hình `local/dev/gcp`, chuyển các data-path và Spark runtime boundary khỏi hardcoded MinIO/local values, đồng thời giữ nguyên kết quả nghiệp vụ của pipeline hiện tại.

**Architecture:** Một runtime-config loader duy nhất resolve YAML profile và environment variables thành immutable dataclasses. Spark session, Delta I/O, validators, benchmark, Feast export và metadata publisher nhận cùng config thay vì tự ghép URI/endpoint. Local Compose vẫn là development profile; GCS profile trở thành đầu vào cho các plan cloud tiếp theo.

**Tech Stack:** Python 3.10, dataclasses, PyYAML, PySpark, Delta Lake, pytest, GCS, MinIO/S3A local adapter.

**Spec:** `docs/superpowers/specs/2026-10-21-cloud-platform-refactor-design.md`

## Global Constraints

- Không đổi fraud logic, Spark transformation semantics, Gold grain, feature formulas hoặc label definition.
- Không provision GCP resource trong plan này.
- Không sinh dataset mới hoặc chạy full four-million/100 GB pipeline.
- Local profile phải tiếp tục dùng được với Docker Compose và dataset nhỏ.
- GCP profile dùng `gs://`; canonical cloud storage không dùng MinIO.
- Không commit secret, ADC file, service-account key, `.env`, runtime data hoặc absolute developer path.
- Mỗi task cập nhật tài liệu/evidence liên quan trong cùng changeset.
- Evidence lịch sử không bị ghi đè; current-truth docs chỉ cập nhật sau validation PASS.
- Chỉ commit sau explicit staged review của đúng task; không dùng `git add .` hoặc `git add -A`.

## Review Focus

1. Missing required environment variable phải fail trước khi Spark được tạo, với tên biến trong error nhưng không lộ giá trị; Task 1 test hành vi này.
2. URI root có hoặc không có dấu `/` cuối phải tạo cùng table URI và không sinh `//`; Task 1 test việc normalize.
3. Local secret không được xuất hiện trong YAML, log hoặc dataclass representation; Task 2 và Task 3 test redaction/required env.
4. `optimized=False` chỉ thay các option optimization, không thay storage connector hoặc business input; Task 3 test config diff chính xác.
5. Metadata/Feast cloud profile không được vô tình tạo fraud-label-to-feature edge hoặc stale local source; Task 7 giữ regression tests cho lineage và FeatureViews.

## Plan series

Design được triển khai bằng các plan độc lập để mỗi phần có thể review và rollback:

1. **Plan này — R0/R1:** environment config và I/O boundaries;
2. **R2:** Terraform GCP foundation và cost controls;
3. **R3:** image catalog, Ansible Jenkins và Helm foundation;
4. **R4:** deterministic sharded generator và 1/10 GB pilots;
5. **R5:** Airflow DP1/DP2/DP3 và DataHub cloud lineage;
6. **R6:** full 100 GB baseline, targeted 100 GB optimization experiments và
   full 100 GB optimized canonical run;
7. **R7:** Feast Online/Redis;
8. **R8/R9:** Kubeflow, MLflow, KServe và fraud API;
9. **R10/R11:** LLM/RAG/Agent, evidence và final demo.

Không bắt đầu plan kế tiếp khi acceptance criteria của plan hiện tại chưa PASS.

---

### Task 1: Runtime config model, interpolation và URI contract

**Files:**

- Create: `data_platform/config/__init__.py`
- Create: `data_platform/config/models.py`
- Create: `data_platform/config/loader.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_runtime_config.py`
- Create: `docs/cloud/configuration.md`

**Interfaces:**

- Consumes: YAML mapping và `Mapping[str, str]` environment values.
- Produces: `load_runtime_config(environment: str | None = None, *, config_root: Path | None = None, environ: Mapping[str, str] | None = None) -> RuntimeConfig`.
- Produces: `interpolate_environment(value: object, environ: Mapping[str, str]) -> object` hỗ trợ `${NAME}` và `${NAME:-default}`.
- Produces: immutable `RuntimeConfig`, `StorageConfig`, `SparkConfig`, `ServiceConfig` dataclasses.
- Produces: `StorageConfig.table_uri(layer: Literal["source", "bronze", "silver", "gold", "feast"], table_name: str) -> str`.

- [ ] **Step 1: Viết failing tests cho environment selection và interpolation**

`tests/unit/test_runtime_config.py` phải có:

- `test_loader_defaults_to_local_when_ewallet_env_is_absent`;
- `test_explicit_environment_overrides_ewallet_env`;
- `test_required_placeholder_names_missing_variable_without_leaking_values`;
- `test_default_placeholder_uses_declared_default`;
- `test_unknown_environment_is_rejected`.

- [ ] **Step 2: Chạy tests để xác nhận FAIL vì package chưa tồn tại**

Run:

```bash
python -m unittest tests.unit.test_runtime_config -v
```

Expected: FAIL với import error cho `data_platform.config`.

- [ ] **Step 3: Viết failing tests cho config schema và URI normalization**

Tests phải assert:

- unknown top-level key bị reject;
- thiếu `storage.gold_root` bị reject;
- `gold_root="gs://bucket/lakehouse/gold/"` tạo
  `gs://bucket/lakehouse/gold/fact_transactions`;
- table name rỗng, absolute hoặc chứa `..` bị reject;
- `repr(RuntimeConfig)` không chứa giá trị secret từ environment.

- [ ] **Step 4: Implement immutable models**

Trong `data_platform/config/models.py` định nghĩa:

```python
@dataclass(frozen=True)
class StorageConfig:
    scheme: str
    source_root: str
    bronze_root: str
    silver_root: str
    gold_root: str
    feast_root: str
    options: Mapping[str, str] = field(default_factory=dict, repr=False)

    def table_uri(self, layer: StorageLayer, table_name: str) -> str: ...

@dataclass(frozen=True)
class SparkConfig:
    master: str | None
    shuffle_partitions: int
    event_log_uri: str
    packages: tuple[str, ...]
    configurations: Mapping[str, str]

@dataclass(frozen=True)
class ServiceConfig:
    datahub_gms_url: str
    trino_host: str | None
    trino_port: int | None

@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    project_id: str | None
    region: str | None
    storage: StorageConfig
    spark: SparkConfig
    services: ServiceConfig
```

Models validate values in `__post_init__`; `options`/config mappings containing
credentials use `repr=False` at their containing field boundary.

- [ ] **Step 5: Implement loader và recursive interpolation**

`loader.py` chọn `<config_root>/<environment>/platform.yaml`, interpolate trước
khi tạo dataclass và reject missing/unknown fields. Error chứa config path và
field/variable name, không chứa complete rendered mapping.

- [ ] **Step 6: Export public interface**

`data_platform/config/__init__.py` chỉ export public dataclasses,
`load_runtime_config` và `interpolate_environment`.

- [ ] **Step 7: Chạy unit tests**

Run:

```bash
python -m unittest tests.unit.test_runtime_config -v
```

Expected: tất cả tests PASS.

- [ ] **Step 8: Viết configuration documentation**

`docs/cloud/configuration.md` giải thích profile selection, placeholder syntax,
source files, preflight errors, secret boundary và ví dụ `table_uri`. Chưa claim
GCS pipeline đã chạy.

- [ ] **Step 9: Review và commit Task 1**

Stage explicit các file được liệt kê trong Task 1, chạy
`git diff --cached --check`, full cached diff và tests. Sau user review:

```bash
git commit -m "feat: add environment runtime configuration"
```

---

### Task 2: Local, dev và GCP profiles cùng config preflight CLI

**Files:**

- Create: `configs/environments/local/platform.yaml`
- Create: `configs/environments/dev/platform.yaml`
- Create: `configs/environments/gcp/platform.yaml`
- Create: `.env.example`
- Create: `data_platform/config/cli.py`
- Create: `tests/contract/__init__.py`
- Create: `tests/contract/test_environment_profiles.py`
- Modify: `docs/cloud/configuration.md`
- Modify: `docs/README.md`

**Interfaces:**

- Consumes: Task 1 `load_runtime_config`.
- Produces: `python -m data_platform.config.cli --environment <name> --check`.
- Produces: stable profiles whose paths/services are consumed by all later tasks.

- [ ] **Step 1: Viết failing profile contract tests**

`tests/contract/test_environment_profiles.py` load cả ba profile bằng fake
environment mapping và assert:

- local roots là `s3a://bronze-zone`, `s3a://silver-zone`,
  `s3a://gold-zone`;
- dev/gcp roots bắt đầu bằng `gs://<provided bucket>/`;
- dev và gcp không có `spark.master="local[...]"`;
- local profile cần `MINIO_ACCESS_KEY` và `MINIO_SECRET_KEY`;
- gcp profile cần `EWALLET_DATA_BUCKET`, `DATAHUB_GMS_URL` và project ID;
- không profile nào chứa literal password, token hoặc developer home path.

- [ ] **Step 2: Chạy test và xác nhận FAIL do profile files chưa tồn tại**

Run:

```bash
python -m unittest tests.contract.test_environment_profiles -v
```

Expected: FAIL với missing `platform.yaml`.

- [ ] **Step 3: Tạo local profile**

Local profile định nghĩa S3A roots, `local[4]`, 32 shuffle partitions, local
Spark event-log URI và S3A connector configuration. Credentials dùng required
placeholders; endpoint có default `http://localhost:9000`.

- [ ] **Step 4: Tạo dev và gcp profiles**

Dev dùng GCS prefix nhỏ/pilot; gcp dùng canonical prefixes từ spec. Cả hai để
Spark master `null`, không khai báo S3A package/credential và nhận region,
project, GCS bucket, DataHub endpoint qua environment.

- [ ] **Step 5: Tạo `.env.example`**

File chỉ chứa variable names và placeholder values, phân nhóm local/GCP. Không
dùng giá trị ADC, access token, billing account, card hoặc real secret.

- [ ] **Step 6: Implement preflight CLI**

`data_platform/config/cli.py` nhận `--environment`, `--check`; success in ra
environment, scheme, redacted roots, project/region và `CONFIG CHECK PASS`.
Failure trả nonzero và actionable message. Không print storage options.

- [ ] **Step 7: Test CLI success/failure**

Thêm tests subprocess hoặc gọi `main(argv, environ)` để assert exit code 0 cho
fake valid gcp env và nonzero khi thiếu bucket.

- [ ] **Step 8: Cập nhật docs index**

Thêm link `docs/cloud/configuration.md` vào `docs/README.md`. Ghi exact preflight
commands cho local/dev/gcp và expected output; chưa mô tả cloud resources là đã
provision.

- [ ] **Step 9: Review và commit Task 2**

Run profile tests, `git diff --cached --check` và secret/path scan. Sau review:

```bash
git commit -m "config: define local and GCP runtime profiles"
```

---

### Task 3: Config-driven SparkSession

**Files:**

- Modify: `data_platform/processing/spark/common/spark_session.py`
- Create: `tests/unit/test_spark_runtime_config.py`
- Modify: `docs/cloud/configuration.md`
- Modify: `docs/07_performance_benchmarks.md`

**Interfaces:**

- Consumes: `RuntimeConfig.spark` và `RuntimeConfig.storage`.
- Produces: `spark_options(config: RuntimeConfig, *, optimized: bool) -> dict[str, str]`.
- Produces: `create_spark_session(app_name: str = "EWallet-Batch-Transformation", optimized: bool = True, runtime_config: RuntimeConfig | None = None) -> SparkSession`.

- [ ] **Step 1: Viết failing pure-option tests**

Tests assert:

- local options chứa S3A endpoint/connector từ local config;
- gcp options không chứa `fs.s3a`, MinIO credential hoặc local master;
- `optimized=True` bật AQE và skew handling;
- `optimized=False` tắt đúng hai option đó nhưng mọi storage option giống nhau;
- event log URI và shuffle partitions đến từ profile;
- empty packages không tạo malformed `spark.jars.packages`.

- [ ] **Step 2: Chạy tests và xác nhận FAIL vì `spark_options` chưa tồn tại**

Run:

```bash
python -m unittest tests.unit.test_spark_runtime_config -v
```

- [ ] **Step 3: Tách pure config construction khỏi Spark creation**

Implement `spark_options`. Không import/start Spark trong pure tests. Delta
extensions/catalog luôn được thêm; environment-specific settings đến từ
runtime config.

- [ ] **Step 4: Refactor `create_spark_session`**

Nếu không truyền config, function gọi `load_runtime_config()`. Chỉ gọi
`.master()` khi config có master. `configure_spark_with_delta_pip` nhận packages
phù hợp; không hardcode S3A. Log environment, scheme, AQE, skew và shuffle
partitions, không log credential/options mapping.

- [ ] **Step 5: Chạy unit và lightweight local Spark smoke**

Run:

```bash
python -m unittest tests.unit.test_runtime_config tests.unit.test_spark_runtime_config -v
EWALLET_ENV=local python -c "from data_platform.processing.spark.common.spark_session import create_spark_session; s=create_spark_session('Config-Smoke'); print(s.range(1).count()); s.stop()"
```

Expected: tests PASS và smoke in `1`. Chỉ chạy smoke khi local MinIO credentials
đã được set; range smoke không yêu cầu đọc dữ liệu.

- [ ] **Step 6: Cập nhật docs nhưng giữ historical measurements**

`docs/07_performance_benchmarks.md` ghi rõ local historical baselines dùng cấu
hình cũ và new runtime profile chưa phải performance optimization. Không sửa
con số benchmark lịch sử.

- [ ] **Step 7: Review và commit Task 3**

Sau cached diff/test review:

```bash
git commit -m "refactor: configure Spark runtime by environment"
```

---

### Task 4: Config-driven Silver/Gold Delta I/O và benchmark inputs

**Files:**

- Modify: `data_platform/processing/spark/silver/delta_silver_io.py`
- Modify: `data_platform/processing/spark/silver/silver_pipeline.py`
- Modify: `data_platform/processing/spark/gold/delta_gold_io.py`
- Modify: `data_platform/processing/spark/gold/gold_pipeline.py`
- Modify: `data_platform/processing/spark/benchmark/high_cardinality_benchmark.py`
- Modify: `data_platform/processing/spark/benchmark/merchant_skew_benchmark.py`
- Create: `tests/unit/test_delta_io_paths.py`
- Modify: `docs/03_batch_pipeline.md`
- Modify: `docs/07_performance_benchmarks.md`

**Interfaces:**

- Consumes: `RuntimeConfig` and `StorageConfig.table_uri`.
- Produces: I/O functions with keyword-only `storage: StorageConfig`.
- Produces: pipeline/benchmark CLI `--environment {local,dev,gcp}`.

- [ ] **Step 1: Viết failing path forwarding tests**

Mock Spark reader/writer và assert:

- `read_bronze(..., storage=config.storage)` load expected Bronze URI;
- `write_silver` save expected Silver URI;
- `read_silver`/`write_gold` use expected roots;
- partition columns vẫn được chuyển nguyên vẹn;
- overwrite/Delta/mergeSchema behavior không đổi.

- [ ] **Step 2: Chạy tests và xác nhận current signatures không nhận storage**

Run:

```bash
python -m unittest tests.unit.test_delta_io_paths -v
```

- [ ] **Step 3: Refactor Silver/Gold I/O signatures**

Loại `BRONZE_ROOT`, `SILVER_ROOT`, `GOLD_ROOT` module constants. Không thêm logic
GCS vào builders; chỉ I/O layer biết physical URI.

- [ ] **Step 4: Load config một lần tại pipeline entrypoint**

`run_silver_pipeline` và `run_gold_pipeline` nhận optional `runtime_config`;
`main` load theo `--environment`. Cùng object được truyền vào Spark session và
mọi I/O call.

- [ ] **Step 5: Refactor benchmark inputs**

Hai benchmark dùng `storage.table_uri` thay hardcoded S3A. CLI và evidence log
ghi environment, dataset URI redacted đến bucket/prefix, Spark config và mode.
Workload/query không đổi.

- [ ] **Step 6: Chạy unit tests và local no-data CLI help/compile**

Run:

```bash
python -m unittest tests.unit.test_delta_io_paths tests.unit.test_spark_runtime_config -v
python -m compileall -q data_platform/config data_platform/processing/spark
python -m data_platform.processing.spark.silver.silver_pipeline --help
python -m data_platform.processing.spark.gold.gold_pipeline --help
```

- [ ] **Step 7: Cập nhật batch và performance docs**

Giải thích I/O boundary, exact commands theo environment và vị trí benchmark
code. Historical results giữ nguyên; cloud benchmark ghi `NOT RUN`.

- [ ] **Step 8: Review và commit Task 4**

```bash
git commit -m "refactor: inject storage paths into Spark pipelines"
```

---

### Task 5: Config-driven Bronze/Silver/Gold validators và logical contracts

**Files:**

- Modify: `data_platform/quality/validate_bronze.py`
- Modify: `data_platform/quality/validate_silver.py`
- Modify: `data_platform/quality/validate_gold.py`
- Modify: `data_platform/contracts/silver_transactions.yml`
- Modify: `data_platform/contracts/silver_fraud_labels.yml`
- Modify: `data_platform/contracts/gold_fact_transactions.yml`
- Modify: `data_platform/contracts/gold_obt_transaction_enriched.yml`
- Modify: `data_platform/contracts/gold_feat_user_90d.yml`
- Modify: `data_platform/contracts/gold_feat_user_behavior.yml`
- Modify: `data_platform/contracts/gold_feat_account_behavior.yml`
- Modify: `data_platform/contracts/gold_feat_device_behavior.yml`
- Modify: `data_platform/contracts/gold_feat_merchant_behavior.yml`
- Create: `tests/contract/test_data_contract_locations.py`
- Create: `tests/unit/test_validator_paths.py`
- Modify: `docs/03_batch_pipeline.md`

**Interfaces:**

- Consumes: shared runtime config.
- Produces: validator CLI `--environment` and structured logical contract
  location fields `storage_layer` plus `storage_table`.

- [ ] **Step 1: Viết failing validator path tests**

Mock Delta read and assert validator read helpers use the supplied Bronze,
Silver và Gold roots. Assert rule names, expected values và validation logic do
not change.

- [ ] **Step 2: Viết failing contract location tests**

Load every contract and assert:

- `storage_layer` matches dataset prefix;
- `storage_table` matches dataset name;
- no `s3a://`, `s3://`, `gs://`, localhost or absolute path exists;
- schema/grain/rules remain present.

- [ ] **Step 3: Refactor validator I/O**

Remove root constants, accept `StorageConfig`, add `--environment`, load config
once. Do not alter validation calculations or success criteria.

- [ ] **Step 4: Convert contracts from physical URI to logical location**

Replace only `storage_location` with `storage_layer` and `storage_table`.
DataHub assertion text continues pointing at same contract files.

- [ ] **Step 5: Run validator/contract tests and compile**

```bash
python -m unittest tests.contract.test_data_contract_locations tests.unit.test_validator_paths -v
python -m compileall -q data_platform/quality
```

- [ ] **Step 6: Update docs**

Document that contracts are environment-independent and runtime config maps a
logical asset to physical URI. Include one Silver and one Gold example.

- [ ] **Step 7: Review và commit Task 5**

```bash
git commit -m "refactor: decouple data validation from storage URIs"
```

---

### Task 6: Storage bootstrap boundaries và local-only MinIO tools

**Files:**

- Modify: `data_platform/storage/scripts/config.py`
- Modify: `data_platform/storage/scripts/init_bucket.py`
- Modify: `data_platform/storage/scripts/init_storage.py`
- Modify: `data_platform/storage/scripts/delta_writer.py`
- Modify: `data_platform/storage/scripts/register_trino_tables.py`
- Modify: `data_platform/storage/scripts/storage_benchmark.py`
- Modify: `data_platform/storage/scripts/minio_client.py`
- Modify: `data_platform/storage/scripts/trino_client.py`
- Create: `tests/unit/test_storage_script_config.py`
- Modify: `docs/cloud/configuration.md`
- Modify: `README.md`

**Interfaces:**

- Consumes: runtime config; local-only commands explicitly require
  `environment == "local"`.
- Produces: backward-compatible inventories (`offline_tables`, `silver_tables`,
  `GOLD_TABLES`) without owning endpoint/path settings.

- [ ] **Step 1: Viết failing boundary tests**

Tests assert:

- importing storage config no longer reads/defaults a MinIO password;
- local bootstrap rejects dev/gcp profile before mutation;
- table inventories including fraud labels remain unchanged;
- Trino settings come from runtime config/environment;
- registration generates URI from profile instead of bucket constants.

- [ ] **Step 2: Move shared inventories out of physical storage settings**

Giữ compatibility re-export ở `storage/scripts/config.py` trong changeset này,
nhưng endpoint/credentials/roots lấy từ runtime config. Không đổi table list.

- [ ] **Step 3: Guard MinIO mutation commands**

`init_bucket.py`, `init_storage.py` và MinIO benchmark fail fast với message
`local-only command` khi profile không phải local. GCP provisioning thuộc
Terraform plan R2.

- [ ] **Step 4: Refactor Trino registration as optional local integration**

Trino command dùng service config và logical table inventories. Nó không được
tự động gọi bởi cloud pipeline trong plan này.

- [ ] **Step 5: Run storage unit tests and command help**

```bash
python -m unittest tests.unit.test_storage_script_config -v
python -m compileall -q data_platform/storage/scripts
python -m data_platform.storage.scripts.init_storage --help
python -m data_platform.storage.scripts.register_trino_tables --help
```

- [ ] **Step 6: Update README/current configuration docs**

Mark Docker/MinIO/Trino commands as local profile only. Add a warning that GCS
is provisioned by Terraform later; do not document a manual bucket workaround.

- [ ] **Step 7: Review và commit Task 6**

```bash
git commit -m "refactor: isolate local storage bootstrap tools"
```

---

### Task 7: Feast and DataHub endpoint/source configuration

**Files:**

- Modify: `ai_platform/features/feast/sources/export_gold_to_parquet.py`
- Modify: `ai_platform/features/feast/sources/export_validation_samples.py`
- Modify: `ai_platform/features/feast/validation/precision_audit.py`
- Modify: `ai_platform/features/feast/feature_repo/feature_definitions.py`
- Create: `ai_platform/features/feast/repository.py`
- Create: `ai_platform/features/feast/feature_repo/feature_store.gcp.yaml`
- Modify: `data_platform/metadata/datahub/lineage/create_lineage.py`
- Modify: `data_platform/metadata/datahub/assertions/publish_assertions.py`
- Create: `tests/unit/test_feast_metadata_runtime_config.py`
- Modify: `docs/ml/feast_offline.md`
- Modify: `docs/08_datahub_lineage.md`

**Interfaces:**

- Consumes: RuntimeConfig storage/services.
- Produces: Feast export CLI `--environment`, environment-specific Feast config
  selection and DataHub publisher `--environment`.
- Produces: `feature_store_config_path(environment: str) -> Path`, trả tracked
  local `feature_store.yaml` hoặc tracked GCP `feature_store.gcp.yaml`.

- [ ] **Step 1: Viết failing Feast source tests**

Tests assert Gold source URI comes from config, timestamp remains
`timestamp[us, UTC]`, manifest records environment/dataset version, and
fraud labels do not appear in FeatureViews. GCS roots must remain `gs://` URIs
and must not be converted by `Path.resolve()` into local paths.

- [ ] **Step 2: Viết failing DataHub endpoint/lineage tests**

Tests assert emitter uses configured GMS URL, Trino verification is optional by
profile, existing eleven direct edges remain, `feat_device_behavior` retains
both actual upstreams, and no fraud-label-to-feature edge exists.

- [ ] **Step 3: Refactor Feast exporters/audits**

Replace hardcoded S3 roots/storage options with runtime config. Update feature
source joining to support both local `Path` roots and object-store URI roots.
Preserve row, key and timestamp parity checks. Generated registry/snapshot
remains ignored.

- [ ] **Step 4: Add environment-specific Feast config selection**

Existing `feature_store.yaml` remains local Dask + null online store for F6
regression. `feature_store.gcp.yaml` points offline artifacts to GCS but keeps
online store disabled until R7; it must not claim Redis is deployed yet.
`feature_store_config_path` rejects unsupported environments and lets callers
select the tracked config without overwriting the local file at runtime.

- [ ] **Step 5: Refactor DataHub publication**

Pass configured endpoint; do not hardcode localhost. Dataset naming remains
logical/stable while physical platform mapping is explicit. Preserve read-back
and existing lineage semantics.

- [ ] **Step 6: Run focused regressions**

```bash
python -m unittest tests.unit.test_feast_metadata_runtime_config -v
python -m unittest tests.test_feast_offline tests.test_historical_fraud_features -v
python -m compileall -q ai_platform/features/feast data_platform/metadata/datahub
```

Expected: configuration tests and existing PIT/feature tests PASS without
starting Trino or DataHub.

- [ ] **Step 7: Update Feast/DataHub docs**

Explain local and future GCP commands, actual lineage mapping, generated
snapshot lifecycle and what is not deployed. Preserve historical F5/F6 data.

- [ ] **Step 8: Review và commit Task 7**

```bash
git commit -m "refactor: configure Feast and DataHub by environment"
```

---

### Task 8: Global stale-literal gate, regression và foundation evidence

**Files:**

- Create: `tests/contract/test_no_runtime_literals.py`
- Create: `ops/validation/validate_runtime_config.py`
- Create: `docs/evidence/cloud/r1_config_foundation.json`
- Modify: `docs/cloud/configuration.md`
- Modify: `docs/README.md`
- Modify: `README.md`

**Interfaces:**

- Consumes: all Task 1-7 public interfaces.
- Produces: one CI-friendly validation command and machine-readable R1 evidence.

- [ ] **Step 1: Viết stale-literal contract test**

Scan operational Python/YAML dưới `data_platform/` và `ai_platform/` rồi fail
với file/line nếu còn:

- `s3a://bronze-zone`, `s3a://silver-zone`, `s3a://gold-zone` ngoài local profile;
- `s3://gold-zone` ngoài historical docs/evidence;
- hardcoded DataHub/Trino localhost ngoài local profile/runtime tools;
- `minioadmin` hoặc developer absolute path.

Allowlist phải explicit cho local Docker Compose, historical docs/evidence và
test fixtures; không allowlist whole directory source code.

- [ ] **Step 2: Implement validation aggregator**

`ops/validation/validate_runtime_config.py` chạy profile parse, stale-literal
scan, contract parse và prints one PASS/FAIL row per check. `main() -> int` trả
nonzero nếu có failure.

- [ ] **Step 3: Run complete lightweight regression**

```bash
python ops/validation/validate_runtime_config.py
python -m compileall -q data_platform ai_platform
python -m unittest \
  tests.test_fraud_generator \
  tests.test_fraud_label_platform \
  tests.test_historical_fraud_features \
  tests.test_feast_offline \
  tests.test_fraud_training_dataset \
  tests.test_fraud_modeling -v
git diff --check
```

Không chạy Spark full data, Trino, DataHub, Redis hoặc GCP resource.

- [ ] **Step 4: Record R1 evidence**

`r1_config_foundation.json` chứa Git SHA, branch, profile check results, literal
scan count, test counts, compile result và timestamp. Không chứa environment
values, token, endpoint credentials hoặc absolute path.

- [ ] **Step 5: Final documentation review**

Current docs phải phân biệt:

- local profile operational;
- dev/gcp configuration defined but cloud resources not provisioned;
- historical MinIO measurements vẫn là historical;
- R2 Terraform là next step;
- Spark cloud optimization và 100 GB chưa chạy.

- [ ] **Step 6: Staged review and final foundation commit**

Stage explicit Task 8 files, inspect cached full diff/evidence và run secret
scan. Sau user review:

```bash
git commit -m "test: enforce cloud runtime configuration boundaries"
```

## Foundation completion gate

Plan R0/R1 chỉ COMPLETE khi:

- [ ] local/dev/gcp profiles parse với explicit test environments;
- [ ] missing config/secret fail trước Spark creation;
- [ ] Spark session và Delta I/O không hardcode MinIO/GCS;
- [ ] Silver/Gold transformations không đổi;
- [ ] validators dùng cùng physical paths với pipelines;
- [ ] contracts là logical/environment-independent;
- [ ] MinIO bootstrap được đánh dấu và guard local-only;
- [ ] Feast/DataHub semantics regression PASS;
- [ ] operational source không còn stale runtime literals ngoài allowlist;
- [ ] current docs và R1 JSON evidence đồng bộ;
- [ ] generated data/runtime artifacts tracked bằng 0;
- [ ] không có cloud resource được tạo;
- [ ] working tree sạch sau các reviewed commits.

Sau gate này, tạo và review plan R2 Terraform. Không bắt đầu Terraform trong
cùng changeset với config foundation.
