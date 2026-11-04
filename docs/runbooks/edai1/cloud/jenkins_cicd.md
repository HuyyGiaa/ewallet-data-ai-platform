# Jenkins Platform CI/CD

## Trạng thái và phạm vi

Root Jenkins orchestration đã được triển khai theo mô hình component-aware và
fail-closed. `Jenkinsfile` chạy profile `platform`; `Jenkinsfile.llm-agent` tiếp
tục là entrypoint tương thích cho profile `llm-agent`. Hai file chỉ checkout và
gọi chung `jenkins/pipeline/componentPipeline.groovy`, vì vậy không có bản sao
logic pipeline theo từng domain.

Đây là trạng thái **source/static ready**, chưa phải Jenkins runtime hoặc CD
runtime verified. Campaign local ngày 2026-11-01 không push image, không gọi
GKE/cloud, không chạy Helm/kubectl và không trigger production.

## Nguồn sự thật và kiến trúc

`jenkins/catalog/components.json` là nguồn sự thật duy nhất cho:

- source-path ownership và Python-project ownership;
- dependency graph, reverse dependency closure và pipeline profile;
- test command, build artifact, deployment target và deploy handler;
- phân loại `active` so với `extension-only`.

Không duy trì một ownership/dependency matrix thủ công trong tài liệu này.
Matrix thực thi được sinh trực tiếp từ catalog tại
[`component-matrix.md`](../../../evidence/ci_cd/local-20261101-final/component-matrix.md).

Luồng orchestration:

1. Xác nhận exact 40-hex reviewed SHA bằng checkout SHA và ghi provenance.
2. Validate catalog, tìm merge-base với target ref rồi tính component scope.
3. Chạy uv lock/sync và command của component được chọn bằng argument list.
4. Ghi manifest, log, lock hash, matrix và `SHA256SUMS`, kể cả khi thất bại.
5. Chỉ khi `DEPLOY_APPROVED=true`, mở Jenkins `input`, ghi identity của người
   approve được Jenkins RBAC cho phép và chạy release policy.
6. Archive evidence trong `finally`; deployment vẫn tắt theo mặc định.

### Change detection

`scripts/ci/change_detection.py` dùng `git merge-base` và diff từ merge-base
đến HEAD, không dùng `HEAD^..HEAD`. Shared path chọn toàn bộ active scope;
thay đổi upstream mở rộng theo reverse dependency closure. Target ref không tồn
tại, không có merge-base hoặc path không ánh xạ đều chọn full active validation
với `full_validation_reason`, thay vì trả scope rỗng.

## Dependency governance bằng uv

Catalog và contract test hiện quản lý đủ 12 project:

- `ai_platform/features/feast`;
- `ai_platform/inference`;
- `ai_platform/ml`;
- `ai_platform/ml/dvc-operator`;
- `ai_platform/ml/pipelines`;
- `ai_platform/serving`;
- `data_platform/metadata/datahub`;
- `infra/docker/flink-benchmark`;
- `infra/docker/llm-agent-tooling`;
- `infra/docker/rag-ingestion`;
- `infra/docker/vllm-cpu-model`;
- `infra/kubernetes/mlflow/image`.

`ai_platform/inference` và `ai_platform/serving` đã được đưa vào dependency
contract coverage. Campaign này không sửa `pyproject.toml`, không sinh hoặc sửa
`uv.lock`, và không thay các protected ML/MLOps pins.

Với mỗi project được chọn, runner chạy theo thứ tự:

```text
uv lock --check
uv sync --locked
component test/import command từ catalog
```

Không có fallback tự cập nhật lock. Thiếu `uv` là
`MISSING_RUNTIME_DEPENDENCY` và làm run thất bại.

## Chạy CI và tái tạo evidence

Từ repository root:

```bash
python3 -m scripts.ci.catalog validate
python3 -m scripts.ci.change_detection \
  --root "$PWD" \
  --head HEAD \
  --target-ref origin/main \
  --output /tmp/ewallet-ci-scope.json
python3 -m scripts.ci.run_ci \
  --root "$PWD" \
  --scope /tmp/ewallet-ci-scope.json \
  --evidence-dir /tmp/ewallet-ci-evidence
(cd /tmp/ewallet-ci-evidence && sha256sum -c SHA256SUMS)
```

Không tái sử dụng một evidence directory cũ. Mỗi run cần directory riêng để
manifest, log và checksum phản ánh đúng một execution.

Trên Jenkins, operator phải truyền `EXPECTED_GIT_SHA` chính xác và một target
ref đã fetch. `TARGET_BRANCH` chỉ dùng để tìm merge-base; pipeline không checkout
hoặc sửa branch theo giá trị này.

## Controlled release

CD bị từ chối trừ khi đồng thời thỏa tất cả điều kiện:

- `DEPLOY_APPROVED=true` và Jenkins `input` trả về approver identity;
- branch khớp `release/<name>`;
- reviewed SHA, checkout SHA và manifest SHA trùng exact 40-hex SHA;
- environment thuộc `dev`, `gcp`, `gke-cpu` và có explicit kube context;
- mọi image reference là `repository@sha256:<64-hex>`;
- component là `active`, có deployment target và deploy handler trong catalog;
- release units theo dependency order.

`scripts/ci/release_policy.py` sinh release plan đã validate;
`scripts/ci/deploy_release.sh` validate lại plan ngay trước dispatch. Dispatcher
chỉ reuse các script đã có cho KFP, MLflow và LLM atomic deploy. Nó không chứa
credential, không chấp nhận mutable `latest` và không chọn kube context mặc định.

`rag-pipeline`, `mcp-agents` và `llm-d-routing` là `extension-only`: catalog
validation cấm deployment target/handler, còn release policy loại chúng khỏi
plan. Một request chỉ gồm extension-only component bị từ chối vì không có unit
có thể deploy.

### Dry-run policy check

Tạo request và immutable image manifest đã review, sau đó:

```bash
python3 -m scripts.ci.release_policy build \
  --request /path/to/release-request.json \
  --image-manifest /path/to/image-digests.json \
  --output /tmp/release-plan.json
scripts/ci/deploy_release.sh --plan /tmp/release-plan.json
```

Không thêm `--execute` khi review local. Chỉ Jenkins gate đã approved mới gọi
dispatcher với `--execute`.

## Rollback và failure semantics

LLM deploy giữ nguyên `helm upgrade --atomic --wait`, nên Helm tự rollback khi
upgrade thất bại. `scripts/llm_agent/rollback.sh` vẫn là manual last-known-good
rollback path và từ chối chạy nếu không chứng minh được revision trước đó.
KFP/MLflow scripts giữ nguyên contract hiện hữu; pipeline mới không tự suy đoán
rollback target hoặc mutate manifest để “sửa” một release thất bại.

Failure được giữ nguyên exit code và phân loại thành
`SOURCE_REGRESSION`, `ENVIRONMENT_FAILURE`,
`MISSING_RUNTIME_DEPENDENCY`, `SECURITY_FAILURE` hoặc
`INFRASTRUCTURE_FAILURE`. Rejected release plan dừng trước external deploy call.

## Evidence ngày 2026-11-01

Actual runner evidence ở
[`local-20261101-final`](../../../evidence/ci_cd/local-20261101-final/manifest.json)
được tạo từ commit `df4118105f6d68d748a03923ea1a08ad1d76bd93` với worktree sạch.
Merge-base scope chọn 9 active components. Run trả `127` tại command đầu tiên
`uv lock --check`, status `FAILED`, classification
`MISSING_RUNTIME_DEPENDENCY`; checksum của toàn bộ artifact đã được verify.

Kết quả static thực tế trước khi viết tài liệu:

- 38 CI contract tests: PASS;
- 12 existing LLM CI/deploy/rollback fake-tool tests: PASS;
- 7 uv dependency contract tests: 6 PASS, 1 SKIP vì thiếu `uv`;
- catalog validate: 12 components, 9 Python projects, PASS;
- Python compileall, JSON parse, Bash syntax và `git diff --check`: PASS.

Verification mở rộng theo command trong catalog ghi nhận: 34 streaming tests
PASS (1 SKIP), 3 MLflow script tests PASS, 12 llm-d tests PASS (5 SKIP).
Nhóm data không chạy đủ vì Python hệ thống thiếu `pandas`; nhóm GKE/LLM có 5
errors và 15 skips do thiếu Helm; RAG source tests và validate-only PASS nhưng
wrapper dừng trước Feast schema check vì thiếu `uv`. Các trường hợp này là giới
hạn runtime thật, không được tính vào các dòng PASS ở trên.

Hai directory trên là snapshot lịch sử bất biến của nhánh CI trước tích hợp;
vì vậy matrix 9 component/9 project và checksum bên trong chúng không được sửa
để phản ánh catalog I5 mới. Tại I5, catalog tích hợp validate 13 component và
12 Python project; 43 CI contract tests cùng 18 compatibility tests PASS.

Các kết quả này không nâng run runner thành `LOCAL_CI_VERIFIED` và không phải
bằng chứng Jenkins/CD runtime.

## Giới hạn môi trường

| Gate | Trạng thái local | Hệ quả |
|---|---|---|
| uv lock/sync | MISSING | Local component runner FAILED; lock consistency runtime chưa verify |
| Helm, kubectl | MISSING | Không lint/template/deploy/readback cluster |
| Jenkins, Groovy, Java | MISSING | Jenkins CPS/runtime syntax chưa verify |
| Trivy | MISSING | Security scan runtime chưa chạy |
| Docker client/server | AVAILABLE | Không build, push hoặc scan image trong campaign này |
| GKE/cloud credentials | NOT USED | Không có cloud operation hoặc production trigger |

Vì vậy trạng thái chính xác là source/static contracts đã pass trong phạm vi
đã chạy; `LOCAL_CI_VERIFIED`, `JENKINS_RUNTIME_VERIFIED` và
`CD_RUNTIME_VERIFIED` vẫn false/unverified.

Jenkins RBAC/Matrix Authorization là cấu hình ngoài repository: source chỉ ghi
identity trả về bởi `input`, không tự chứng minh identity đó là owner. Trước CD
runtime, operator phải giới hạn quyền approve cho nhóm owner được phê duyệt và
thu thập readback cấu hình Jenkins; thiếu gate này thì không được chạy release.

## Mapping yêu cầu triển khai

| Yêu cầu | Implementation/evidence |
|---|---|
| Root modular orchestration | `Jenkinsfile`, shared Groovy helper, compatibility profile |
| Catalog single source | `jenkins/catalog/components.json`, generated matrix |
| Multi-commit detection | merge-base detector + fallback full validation tests |
| 9-project uv governance | catalog, dependency contract và unchanged dependency-file diff |
| Execution-bound evidence | runner manifest/log/checksum, gồm actual failed run |
| CD default deny | Jenkins boolean default false + release policy + owner input |
| Immutable/reviewed release | exact SHA, release branch, digest-only references, explicit context |
| Extension-only exclusion | catalog validation và release policy tests |
| ML/LLM compatibility | existing deployment scripts reused; LLM fake-tool tests pass |
