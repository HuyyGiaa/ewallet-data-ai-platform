# Terraform GCP Foundation

## Trạng thái

R2 Terraform source đã được triển khai và validate. Bootstrap plan hiện chỉ có
một resource: GCS bucket lưu Terraform state. Chưa có `terraform apply`, vì vậy
chưa có resource mới hoặc chi phí cloud nào được tạo bởi R2.

Project và vị trí đã khóa:

```text
project: ewallet-data-ai-platform-hg26
region:  asia-southeast1
zone:    asia-southeast1-b
```

Terraform không quản lý budget/alert đã tạo trên GCP Console. Nó cũng không
chạy generator, Spark, Airflow, Feast, ML hoặc Kubernetes application.

## Terraform tạo những gì

```text
bootstrap
└── private versioned GCS state bucket

environments/dev
├── required APIs
├── custom VPC + regional subnet + Private Google Access
├── protected canonical data bucket
├── seven-day temporary/staging bucket
├── Docker Artifact Registry
├── workload service accounts + least-privilege IAM
├── Dataproc Serverless batch prerequisites
├── optional GKE cluster and CPU node pool
└── optional Jenkins VM with IAP-only SSH
```

GKE và Jenkins có source code đầy đủ nhưng bị cost gate mặc định:

```hcl
enable_gke        = false
enable_jenkins_vm = false
```

Vì vậy foundation apply đầu tiên không tạo VM, GKE cluster hay node pool.
Dataproc dùng serverless batch: khai báo R2 không giữ Spark cluster chạy idle;
chi phí compute chỉ xuất hiện khi một batch thực sự được submit ở bước sau.

## Identity: ADC khác service account thế nào

**Application Default Credentials (ADC)** là credential người dùng trên laptop
được Terraform/Google SDK tự tìm thấy sau lệnh:

```bash
gcloud auth application-default login
```

ADC chỉ dùng để người vận hành tạo và đọc hạ tầng trong lúc development. File
ADC nằm ngoài repository, không copy vào Git hoặc container.

Service account là identity của workload sau khi deploy. Generator, Spark,
training, serving, GKE nodes và CI có account riêng. Terraform không tạo JSON
service-account key; workload dùng identity gắn vào runtime và về sau dùng GKE
Workload Identity.

## Cổng kiểm tra không tạo resource

Từ repository root:

```bash
bash ops/validation/validate_terraform_foundation.sh
```

Lệnh này chỉ format-check, init provider/module với backend tắt, validate HCL và
chạy contract tests. Nó không chạy `terraform apply`.

## Bước 1: bootstrap remote state

Từ clean checkout:

```bash
cd infra/terraform/gcp/bootstrap
terraform init
terraform validate
terraform plan -out=bootstrap.tfplan
terraform show -no-color bootstrap.tfplan
```

Plan được chấp nhận khi kết thúc bằng:

```text
Plan: 1 to add, 0 to change, 0 to destroy.
```

Resource duy nhất phải là
`google_storage_bucket.terraform_state`. Bucket có versioning, public-access
prevention, `force_destroy = false`, deletion policy `PREVENT` và Terraform
`prevent_destroy`.

Đây là checkpoint tạo resource đầu tiên. Sau khi review đúng plan, người vận
hành chạy:

```bash
terraform apply bootstrap.tfplan
```

Không dùng `-auto-approve`. Plan binary và local state bị Git ignore.

## Bước 2: init dev stack bằng remote state

Chỉ chạy sau khi bootstrap apply thành công:

```bash
cd ../environments/dev
terraform init -reconfigure \
  -backend-config="bucket=ewallet-data-ai-platform-hg26-tfstate" \
  -backend-config="prefix=terraform/dev"
terraform validate
terraform plan -out=dev-no-compute.tfplan
terraform show -no-color dev-no-compute.tfplan
```

Trước apply, kiểm tra plan:

- project chỉ là `ewallet-data-ai-platform-hg26`;
- `0` resource `google_container_cluster`;
- `0` resource `google_container_node_pool`;
- `0` resource `google_compute_instance.jenkins`;
- không có action destroy;
- không có `google_billing_budget`;
- canonical bucket và state bucket không bị xóa.

Chưa chạy `terraform apply dev-no-compute.tfplan` cho đến khi plan đã được ghi
evidence và review.

## Runtime outputs

Sau foundation apply, lấy output bằng:

```bash
terraform output
terraform output -raw data_bucket_name
terraform output -raw artifact_registry_url
```

Runtime config nhận bucket/project/region qua environment, không chép giá trị
vào Python:

```bash
export GCP_PROJECT_ID="$(terraform output -raw project_id)"
export GCP_REGION="$(terraform output -raw region)"
export EWALLET_DATA_BUCKET="$(terraform output -raw data_bucket_name)"
```

`DATAHUB_GMS_URL` chỉ được set sau khi DataHub được deploy; R2 không dựng
DataHub.

## Khi nào mới bật GKE và Jenkins

GKE/Jenkins chỉ bật ở R3 sau khi foundation plan/apply và cost estimate được
review. Tạo file `terraform.tfvars` local (file này bị ignore):

```hcl
enable_gke        = true
enable_jenkins_vm = true
gke_master_authorized_cidrs = ["YOUR_PUBLIC_IP/32"]
```

Sau đó phải chạy một `terraform plan` mới. GKE mặc định là zonal, CPU-only,
`e2-standard-4`, autoscale 0..3; Jenkins mặc định `e2-standard-2`. Jenkins dùng
OS Login và SSH qua IAP range, không commit private key.

GKE từ chối `enable_gke=true` nếu chưa có ít nhất một CIDR quản trị và cấm
`0.0.0.0/0`. Workload Identity bindings đã dành sẵn cho ba Kubernetes service
account: `data-platform/generator`, `ml-platform/training` và
`serving/fraud-inference`; Helm R3 phải dùng đúng tên này thay vì quyền node.

## Scale down và destroy

Budget alert chỉ cảnh báo; nó không tự dừng resource. Khi không demo:

1. scale application về 0 bằng Helm/Kubernetes runbook ở R3;
2. giữ resource enabled nhưng đặt `jenkins_deletion_protection = false`, review
   và apply plan chỉ thay protection;
3. đặt `enable_jenkins_vm = false`, tạo/review plan thứ hai rồi mới bỏ VM;
4. sau khi backup stateful workload, làm tương tự với
   `gke_deletion_protection = false` trong plan thứ nhất;
5. đặt `enable_gke = false` trong plan thứ hai để bỏ cluster/node pool.

Không chạy mù `terraform destroy`. Canonical/state buckets cố ý chặn destroy.
Muốn xóa hoàn toàn phải review dữ liệu, export evidence, bỏ
`deletion_policy = "PREVENT"` và `prevent_destroy`, tạo plan riêng rồi mới xóa.
Temporary bucket có lifecycle bảy ngày nhưng `terraform destroy` vẫn cần plan
review.

## Các lỗi thường gặp

| Lỗi | Nguyên nhân | Cách xử lý |
|---|---|---|
| `Backend initialization required` | Dev stack chưa trỏ vào state bucket | Apply bootstrap rồi chạy `terraform init -reconfigure` với bucket/prefix ở trên |
| `403` khi plan/apply | ADC user thiếu quyền trên project | Kiểm tra `gcloud auth list`, project active và IAM; không tạo JSON key để lách lỗi |
| bucket name already exists | Tên GCS là global | Chọn override duy nhất trong local `.tfvars`, không hardcode credential |
| deletion protection error | Terraform đang cố xóa resource bảo vệ | Dừng và review plan; đây là guardrail đúng |
| plan có GKE/Jenkins ngoài ý muốn | Cost flag local đang bật | Đặt lại hai `enable_* = false` và tạo plan mới |

## Giới hạn hiện tại

- Bootstrap state bucket chưa apply.
- Dev remote backend chưa init vì state bucket chưa tồn tại.
- GCS canonical data chưa có dữ liệu.
- Delta-on-GCS/Dataproc smoke chưa chạy.
- GKE, Jenkins, Helm, Ansible và 100 GB pipeline chưa bắt đầu.
