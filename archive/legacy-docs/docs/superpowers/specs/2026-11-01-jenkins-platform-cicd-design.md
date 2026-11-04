# Jenkins Platform CI/CD Design

## Intent

Xây dựng nền tảng Jenkins CI/CD thực dụng, tái lập được cho E-Wallet
Data & AI Platform trên monorepo hiện hữu. Pipeline phải chỉ chạy các gate có
triển khai thật, giữ nguyên các hợp đồng ML/MLOps đã được duyệt, và
không biến source-only validation thành bằng chứng runtime.

Người sử dụng chính là chủ repository và Jenkins operator. Kết quả cần
hỗ trợ CI theo component ngay bây giờ, đồng thời cung cấp extension contract
cho các miền chưa sẵn sàng mà không tuyên bố chúng có thể deploy.

## Goals

- Có root `Jenkinsfile` là entrypoint orchestration duy nhất cho pipeline nền tảng.
- Dùng catalog khai báo component, source ownership, shared dependencies, test
  commands, build artifacts và deployment targets.
- Tính diff từ merge-base với target branch; full-validation fail-safe khi không
  chứng minh được phạm vi thay đổi.
- Audit và kiểm tra đủ chín Python project dùng `uv`, không regenerate
  lockfile và không trộn môi trường.
- Có provenance, failure classification, JUnit/evidence manifest và checksum có
  thể lưu trữ trên mọi kết quả, kể cả failure.
- Chuẩn bị CD có kiểm soát, tắt mặc định, chỉ nhận immutable Git
  SHA và image digest, với approval và target environment hiển thị rõ.
- Giữ `Jenkinsfile.llm-agent` là entrypoint tương thích cho job cũ và tái
  sử dụng cùng catalog/runner thay vì tách thêm logic thứ hai.

## Non-goals

- Không triển khai business logic cho ML, G1, RAG, MCP hoặc Agent.
- Không thay đổi MLflow promotion, Feast registry/materialization, KFP, KServe
  hay Fraud API runtime contracts.
- Không deploy GKE/cloud, push image, chạy production KFP/MLflow hoặc khởi
  động Jenkins runtime trong campaign này.
- Không sửa Observability configuration, dashboard, metrics, logging hoặc tracing.
- Không cấp runtime verification nếu chỉ có static/local evidence.

## Anti-goals

- Decorative stages luôn trả thành công.
- Dùng `HEAD^..HEAD` là diff range duy nhất.
- Che exit code bằng `tee`, shell pipeline hoặc `|| true`.
- Tự sinh hay sửa tay `uv.lock`, nâng version để làm CI xanh.
- Dùng mutable `latest`, thông tin xác thực trong source/evidence, hoặc
  Docker socket không có policy gate.
- Tự động production deploy từ development branch.

## Constraints and authoritative state

- Workspace duy nhất: `/home/huyy-giaa/Develop/ewallet-pc-cicd`.
- Branch duy nhất: `codex/pc-cicd-20261101`.
- Base: `e7dbbc27436faef5b06ba4c40df66a1639f46a28`.
- Không chạm worktree `/home/huyy-giaa/Develop/ewallet-pc-observability`.
- Kiến trúc repository và Living Decision snapshot trong `docs/` là fallback
  authoritative state khi không có Google Docs link/readback trong task.
- Local host hiện không có `uv`, Helm, kubectl, Jenkins/Groovy và Trivy.
  Kết quả phải phân loại `STATIC_VALIDATION_PASS`,
  `LOCAL_CI_VERIFIED`, `JENKINS_RUNTIME_VERIFIED` và `CD_RUNTIME_VERIFIED`
  theo evidence thực tế.

## Approaches considered

### 1. Root orchestrator plus declarative component catalog — selected

Root `Jenkinsfile` thực hiện stage orchestration. Catalog dữ liệu là nguồn
sự thật cho component ownership và dependency graph; các runner nhỏ thực thi
validation, test, build-plan, release gate và evidence. Cách này dễ test offline,
không hard-code hàng chục nhánh trong Groovy và phù hợp monorepo.

### 2. Extend only `Jenkinsfile.llm-agent` — rejected

Ít file mới hơn nhưng tiếp tục gắn pipeline toàn nền tảng vào tên/job
thuộc LLM. Khó tách ownership ML, data, serving và future extension.

### 3. One large root Jenkinsfile — rejected

Dễ nhìn ban đầu nhưng lặp mapping, test commands và deploy order trong
Groovy; khó unit-test và dễ tạo decorative stages.

## Architecture

### Entry points and shared runner

- `Jenkinsfile` là authoritative root entrypoint với các stage checkout,
  provenance, detect, validate, test, render/build validation, security,
  evidence và gated release.
- `Jenkinsfile.llm-agent` trở thành compatibility entrypoint giới hạn selected
  profile vào nhóm LLM/Agent. Nó không chứa component mapping/test/build/deploy
  implementation riêng.
- Hai entrypoint gọi cùng pipeline helper và cùng CLI runners. Phần wrapper
  chỉ được phép khác profile/default parameters.

### Catalog and dependency graph

Catalog được version trong `jenkins/catalog/` và validate fail-closed. Mỗi
component có:

- stable name và maturity (`active` hoặc `extension-only`);
- source/include paths và shared dependency paths;
- upstream component dependencies;
- Python project ownership nếu có;
- reproducible source, unit, integration/contract, YAML/Helm commands;
- build artifact và deployment target chỉ khi repository có triển khai thật;
- deployability flag để future components không thể vô tình deploy.

Catalog ban đầu bao phủ data platform, streaming, Feast, ML training/KFP,
MLflow, KServe serving, Fraud API, G1/LLM platform và các extension-only RAG,
MCP, llm-d, kagent/Agent. Matrix tài liệu được sinh/kiểm tra từ chính
catalog để tránh hai nguồn sự thật.

### Change detection

1. Xác minh checkout là commit SHA đầy đủ và ghi provenance.
2. Chọn target branch từ Jenkins change target hoặc tham số đã validate.
3. Fetch ref cần thiết mà không checkout/switch branch.
4. Tính `git merge-base HEAD <target-ref>` và diff `<base>...HEAD`.
5. Map file đổi sang component, sau đó lan truyền theo shared paths và
   reverse dependency graph.
6. Nếu target/ref/merge-base/catalog không xác định được, chọn tất cả
   active components và ghi `full_validation_reason`; không trả danh sách rỗng.
7. Manual component selection chỉ được phép mở rộng phạm vi, không
   được thu hẹp diff-derived scope.

### UV dependency governance

Chín project được audit độc lập:

1. `ai_platform/features/feast`
2. `ai_platform/inference`
3. `ai_platform/ml`
4. `ai_platform/ml/dvc-operator`
5. `ai_platform/ml/pipelines`
6. `ai_platform/serving`
7. `infra/docker/llm-agent-tooling`
8. `infra/docker/vllm-cpu-model`
9. `infra/kubernetes/mlflow/image`

`ai_platform/inference` và `ai_platform/serving` không thiếu lockfile; chúng
thiếu coverage trong contract registry/test hiện tại. Thay đổi tối thiểu là
bổ sung hai project vào authoritative project inventory, không thay
`pyproject.toml` hoặc `uv.lock`.

Với mỗi project được chọn, CI chạy `uv lock --check` trước,
`uv sync --locked` trong môi trường riêng, sau đó mới chạy test/import
command của project. CI không chạy `uv lock` không có `--check` và không
copy dependency giữa các environment.

### CI stages and failure semantics

Chỉ stage có command thật mới xuất hiện:

1. Checkout/provenance.
2. Catalog/source validation và change detection.
3. UV lock check và locked sync cho selected projects.
4. Compilation/import validation.
5. Unit tests.
6. Integration/contract tests.
7. YAML validation khi parser khả dụng.
8. Helm lint/template khi Helm khả dụng; thiếu tool là
   `MISSING_RUNTIME_DEPENDENCY`, không phải PASS.
9. Docker build validation cho selected buildable images khi Docker khả dụng.
10. Security gates có tool và policy thật.
11. JUnit/report publication và evidence archival.

Shell runner dùng `set -euo pipefail`; nếu cần `tee` phải giữ `pipefail`.
Failure manifest phân loại `SOURCE_REGRESSION`, `ENVIRONMENT_FAILURE`,
`MISSING_RUNTIME_DEPENDENCY`, `SECURITY_FAILURE` hoặc
`INFRASTRUCTURE_FAILURE`, kèm stage, command, exit code và log path.

### Controlled CD

CD tắt mặc định và cần đồng thời:

- explicit release branch match;
- `DEPLOY_APPROVED=true` cùng Jenkins `input`/owner approval;
- exact 40-hex Git SHA bằng checkout SHA;
- target environment nằm trong allowlist;
- image manifest chỉ chứa `repository@sha256:<64-hex>`;
- pre-deploy readback và deployment identity;
- dependency-ordered deploy, post-deploy smoke và evidence;
- rollback target được chứng minh từ last-known-good evidence.

Local campaign này chỉ kiểm tra source contract. Nó không mở deploy gate,
không login registry, không push và không gọi kubectl/Helm trên cluster.
Docker socket không được mount tự do bởi pipeline mới; scanner phải
quét OCI archive/reference qua executor được operator phê duyệt. Script LLM
cũ có socket mount được ghi là known limitation và không được root
pipeline gọi trong chế độ local/static.

### Evidence model

Mỗi run tạo manifest JSON có schema version với branch, SHA, commit message,
build number, trigger, UTC timestamp, merge base, changed files/components, lock
SHA-256, command result, artifact digest và deployment identity nếu có. Mọi file
evidence có SHA-256 trong checksum manifest.

Status tách biệt:

- `SOURCE_READY`: source/catalog/contracts hiện hữu.
- `STATIC_VALIDATION_PASS`: parser/contract/unit static gate đã chạy.
- `LOCAL_CI_VERIFIED`: toàn bộ local commands được yêu cầu đã chạy.
- `JENKINS_RUNTIME_VERIFIED`: chỉ khi có real Jenkins build evidence.
- `CD_RUNTIME_VERIFIED`: chỉ khi có approved deploy/readback/smoke/rollback
  evidence.

## Planned file boundaries

CI/CD-owned files dự kiến:

- `Jenkinsfile`: root orchestration entrypoint.
- `Jenkinsfile.llm-agent`: compatibility profile entrypoint.
- `jenkins/catalog/components.json`: component/dependency/build/deploy catalog.
- `jenkins/pipeline/*.groovy`: shared Jenkins orchestration helpers.
- `scripts/ci/*.py` và `scripts/ci/*.sh`: catalog, diff, execution, evidence và
  release-policy runners.
- `tests/ci/*`: offline unit/contract tests với fake external tools.
- `tests/unit/test_uv_dependency_contract.py`: inventory coverage cho chín project.
- `docs/cloud/jenkins_cicd.md`: architecture, Jenkins, release/rollback,
  reproduction, rubric mapping và limitations theo taxonomy cloud operations
  hiện hữu; component matrix được sinh từ catalog khi validation chạy.
- `docs/evidence/ci_cd/<run-id>/`: chỉ evidence do local execution thật tạo.

Không sửa source/config thuộc ML, G1, RAG, Agent hoặc Observability.

## Validation strategy

- TDD cho catalog validation, merge-base diff/fallback, dependency closure,
  release gate, failure classification và evidence checksum.
- Baseline regression ghi rõ pre-existing failure do hai project uv bị bỏ sót.
- Chạy `git diff --check`, Python compile, targeted CI tests, existing
  `tests.llm_agent.test_ci_scripts` và uv contract tests.
- Chạy `uv lock --check`/`uv sync --locked` chỉ khi tool có thật; nếu không,
  báo `MISSING_RUNTIME_DEPENDENCY` và không claim local uv PASS.
- Helm/Jenkins/Trivy runtime tương tự: static contract có thể PASS, runtime
  verification vẫn UNVERIFIED.
- Trước commit: review full status/diff, chạy lại strongest practical suite,
  sau đó tạo focused conventional commit trên nhánh hiện tại.

## Documentation and handoff

Bàn giao cuối bằng tiếng Việt gồm commit SHA, file thay đổi, test và
tool limitations, evidence paths/checksums, known limitations và Living Decision
entry copy-ready. Không merge, push, rebase hoặc thay đổi main/canonical.
