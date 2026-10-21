# R3 Dataproc Delta-on-GCS Smoke

## Mục tiêu

Checkpoint này trả lời một câu hỏi kiến trúc duy nhất trước khi chạy dữ liệu
lớn: **Dataproc Serverless có chạy đúng Delta Lake trên GCS của project hay
không?**

Smoke không chạy generator, Bronze, Silver, Gold hoặc 100 GB. Nó tạo sáu dòng
cố định trong temporary bucket, thực hiện:

```text
write 4 rows as Delta version 0
  -> append 2 rows as Delta version 1
  -> read Delta from GCS
  -> validate rows, IDs, amount, schema and microsecond timestamp
  -> write evidence.json
```

Nếu smoke PASS, Dataproc Serverless trở thành runtime Spark chính cho các pilot.
Nếu smoke FAIL vì incompatibility của Delta/runtime sau khi IAM và network đã
đúng, Spark Operator trên GKE là fallback đã có trong design.

## Vì sao pin runtime 2.2

R3 pin Dataproc runtime `2.2`. Runtime này dùng Spark 3.5 và có `delta-spark`
3.2 trong Python environment cùng GCS connector. Delta Python package không tự
đưa Delta JVM classes vào Spark classpath. Submission vì vậy pin và checksum
ba JAR tương thích với Spark 3.5/Scala 2.13: `delta-spark` 3.2.0,
`delta-storage` 3.2.0 và `antlr4-runtime` 4.9.3. `gcloud` upload các JAR local
qua dependency bucket rồi gắn chúng vào riêng batch bằng `--jars`.

Không dùng `spark.jars.packages` trong cloud smoke. Serverless VM chỉ có
internal IP và subnet hiện chưa có Cloud NAT, nên tải Maven dependency từ bên
trong Spark runtime không phải đường reproducible. Đây là cặp tương thích với
pipeline Spark 3 hiện tại.
Không dùng runtime 3.0 trong checkpoint này vì runtime đó dùng Spark 4 và sẽ
trộn một migration framework vào bài kiểm tra storage.

Tham khảo chính thức:

- [Managed Service for Apache Spark runtime 2.2](https://docs.cloud.google.com/managed-spark/docs/concepts/versions/spark-runtime-2.2)
- [Serverless Spark properties](https://docs.cloud.google.com/managed-spark/docs/concepts/spark-properties-serverless)
- [gcloud submit PySpark batch](https://docs.cloud.google.com/sdk/gcloud/reference/dataproc/batches/submit/pyspark)

## Guardrails

- CLI mặc định là dry-run; chỉ `--execute` mới tạo batch.
- Batch dùng `dataproc.tier=standard` và TTL `20m`.
- Delta JAR được tải vào thư mục generated `tmp/dataproc-delta-dependencies/`,
  kiểm tra SHA-256 và không được Git track.
- Project, region, subnet, temporary bucket và Spark service account được đọc
  từ Terraform outputs; người chạy không copy ID thủ công.
- Cùng temporary bucket được truyền dưới dạng URI `gs://...` cho
  `--deps-bucket` để upload file Python local, và dưới dạng tên bucket thuần
  cho `--staging-bucket` để lưu cấu hình cùng driver output của batch.
- Output nằm dưới `gs://<temporary-bucket>/dataproc-smoke/delta/<run-id>` và
  chịu lifecycle cleanup của temporary bucket.
- Mỗi run dùng prefix mới và Delta write `errorifexists`, nên không ghi đè run
  cũ.
- Không dùng service-account JSON key. Batch chạy bằng
  `ewallet-spark@<project>.iam.gserviceaccount.com`.
- Không bật GKE, Jenkins, Trino hoặc MinIO.

## Kiểm tra local trước khi submit

Từ repository root và Conda environment của project:

```bash
python -m unittest \
  tests.unit.test_dataproc_delta_smoke \
  tests.unit.test_submit_dataproc_delta_smoke -v
```

Tạo một run ID mới rồi render lệnh:

```bash
RUN_ID="r3-smoke-$(date -u +%Y%m%d-%H%M%S)"
python -m ops.gcp.submit_dataproc_delta_smoke --run-id "$RUN_ID"
```

Output phải bắt đầu bằng `DRY RUN`. Bước này chỉ đọc Terraform remote state và
không tạo cloud resource.

## Checkpoint do người vận hành chạy

Sau khi review lệnh dry-run, submit đúng một batch:

```bash
python -m ops.gcp.submit_dataproc_delta_smoke \
  --run-id "$RUN_ID" \
  --execute
```

Lệnh chờ batch kết thúc, đọc `<run-uri>/evidence.json`, validate contract, rồi
mới ghi bản evidence đã xác thực vào:

```text
docs/evidence/cloud/r3_dataproc_delta_smoke.json
```

PASS bắt buộc gồm:

| Check | Expected |
|---|---:|
| Initial rows | 4 |
| Final rows | 6 |
| Distinct transaction IDs | 6 |
| Amount sum | 210.75 |
| Delta versions | 0 → 1 |
| Microsecond timestamp | `2026-01-01T00:00:00.123456+00:00` |
| Schema | string, string, double, timestamp |

## Quan sát trên Google Cloud UI

Trong Console, chọn đúng project rồi mở **Dataproc → Batches**. Chọn batch có
tên bằng `$RUN_ID` để xem:

- trạng thái `Pending`, `Running`, `Succeeded` hoặc `Failed`;
- runtime version và service account;
- driver output;
- Spark UI khi batch còn retention;
- thời gian bắt đầu/kết thúc và lỗi IAM/network/runtime nếu có.

CLI tương đương để đọc lại:

```bash
gcloud dataproc batches describe "$RUN_ID" \
  --project=ewallet-data-ai-platform-hg26 \
  --region=asia-southeast1
```

## Cleanup

Temporary bucket tự dọn object theo lifecycle bảy ngày. Sau khi evidence local
đã được validate, có thể dọn run sớm bằng:

```bash
gcloud storage rm --recursive \
  "gs://ewallet-data-ai-platform-hg26-temp/dataproc-smoke/delta/$RUN_ID"
```

Không xóa canonical bucket, Terraform state hoặc prefix của run khác.

## Trạng thái hiện tại

- Source smoke job: **IMPLEMENTED**
- Unit contract: **9/9 PASS**
- Terraform-derived dry-run: **PASS**
- Cloud batch `r3-smoke-20261021-185549`: **FAILED — DELTA JVM JARS MISSING**
- Pinned Delta JAR submission fix: **PASS**
- Cloud batch `r3-smoke-20261021-190532`: **SUCCEEDED**
- Resolved runtime: **2.2.89 / Spark 3.5.3 / Scala 2.13 / Delta 3.2.0**
- Spark computation duration: **32.333 seconds**
- Delta read-back: **4 → 6 rows, version 0 → 1**
- Timestamp precision: **PASS — 123456 microseconds preserved**
- Dataproc versus Spark Operator decision: **DATAPROC SERVERLESS SELECTED**

Machine-readable evidence:

```text
docs/evidence/cloud/r3_dataproc_delta_smoke.json
```

Batch state và remote evidence đã được đọc lại trực tiếp từ GCP sau khi job
kết thúc. Smoke này xác nhận compatibility và correctness cho bounded pilot;
nó chưa phải performance benchmark và không đại diện throughput của pipeline
100 GB.
