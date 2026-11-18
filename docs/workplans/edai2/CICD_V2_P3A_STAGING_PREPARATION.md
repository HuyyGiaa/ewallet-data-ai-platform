# CI/CD V2-P3A — Chuẩn bị Agent staging (offline) và runbook V2-P3B

| Mục | Giá trị |
|---|---|
| Trạng thái | `V2_P3A_OFFLINE_READY` (chuẩn bị xong; mọi điểm runtime ghi `PENDING_RUNTIME_VERIFICATION`) |
| Ngày | 2026-11-18 |
| Nhánh | `feature/edai2-cicd-v2`, base `363f083d` |
| Phạm vi | Offline. Không GKE, Jenkins, Cloud Build, kubectl hay Helm vào cluster. Giữ kiến trúc BYO. |

## 1. Lựa chọn kiến trúc

**Chọn Option A: namespace sandbox riêng `ewallet-sandbox-staging`**, cùng với `ewallet-agents-staging` cho các service.

Option B (`kagent-pilot`) bị loại vì repo cho thấy namespace này không cô lập:
- Có sẵn pool `kagent-pilot` dùng release ateom image, `pilot-sandbox-agent`, mock OpenAI và policy riêng (`infra/kubernetes/kagent-pilot/manifests/`).
- Worker identity `cluster.local/ns/kagent-pilot/sa/default` sẽ dùng chung với worker pilot. WorkerPool không có `serviceAccountName`, nên namespace chính là identity.
- `kagent-pilot-worker-egress` giới hạn egress của worker pilot; muốn dùng thì phải sửa policy của pilot.
- Quyền sở hữu khi dọn dẹp không rõ ràng.

| Câu hỏi | Trả lời |
|---|---|
| Namespace | `ewallet-agents-staging` (Helm release `llm-agent-staging`) và `ewallet-sandbox-staging` (WorkerPool `staging-agents` + 2 SandboxAgent) |
| Thay đổi cấu hình cần thiết | Overlay `infra/kubernetes/kagent-pilot/values/kagent-sandbox-watch-overlay.yaml`: thêm **cả hai** namespace staging vào `controller.watchNamespaces` và `rbac.namespaces`. Post-renderer phải cho `ewallet-sandbox-staging` quyền env-source (SandboxAgent đọc `secretKeyRef`). Áp dụng bằng công cụ có sẵn `scripts/llm_agent/sandbox_watch.sh` |
| Có restart controller không? | **Có**: controller kagent dùng chung với production sẽ restart (thay đổi ConfigMap `WATCH_NAMESPACES`). Không thực hiện trong V2-P3A |
| Còn phải xác minh runtime | Thời gian gián đoạn reconcile của production khi restart; SandboxAgent production vẫn Ready sau restart |
| Owner cần duyệt | Nâng cấp watch scope trong một cửa sổ có kiểm soát (quyết định L1 của V2-P0) |

**Thành phần:**
- Coordinator vẫn là kagent `Agent` BYO, render bởi Helm.
- Hai Specialist là `SandboxAgent` trên một WorkerPool staging dùng chung cho cả hai.
- MCP servers, MCP Gateway, A2A Gateway và idempotency Redis nằm trong release staging.
- Phần dùng chung chỉ là control plane: kagent controller và Substrate `ate-system`.

## 2. File thay đổi

| File | Lý do |
|---|---|
| `scripts/ci/agent_staging.py` (mới) | Executor của kind `agent-staging`: preflight, Helm, fleet, chờ ready, verify, cleanup. Ghép các module có sẵn, không viết lại |
| `jenkins/cd/agent-staging.contract.json` (mới) | Namespace, release, Secret/ConfigMap bắt buộc, fixture và known answers (lấy lại từ `sandbox-candidate.contract.json`) |
| `jenkins/catalog/release-units.json` | Unit `agent-staging` (stage `rehearsal`) và group `release/agent-staging` |
| `scripts/ci/release_units.py` | Kind `agent-staging`: chỉ được nhắm `ewallet-agents-staging`, với đúng 2 image, và contract phải hợp lệ |
| `scripts/ci/release_dispatch.py` | `commands_for` thêm nhánh cho kind mới (truyền SHA, image, manifest, context của plan) |
| `infra/kubernetes/jenkins/jenkins.yaml` | Job `edai2-cd-agent-staging` cho `*/release/agent-staging` (giống các job release khác) |
| `scripts/ci/sandbox_agents.py` | Hàm `assess_*` nhận pool làm tham số; mặc định vẫn là production |
| `scripts/ci/sandbox_candidate.py` | `kubectl_forward` bỏ `--context` khi chạy bằng identity in-cluster |
| `infra/kubernetes/agent-staging/fleet.yaml.tmpl` (mới) | WorkerPool staging, 2 SA, 2 SandboxAgent, sinh ra từ fleet production |
| `infra/kubernetes/agent-staging/namespaces.yaml` (mới) | 2 namespace (ambient, PSA), SA default không có token, ResourceQuota. Operator apply một lần |
| `infra/helm/llm-agent-platform/values-staging.yaml`, `templates/staging-guard.yaml` | Flow ingress để worker staging gọi MCP Gateway staging; guard cho phép peer `ewallet-sandbox-staging` |
| `infra/kubernetes/jenkins/rbac/agent-staging-cd.proposed.yaml` (mới) | RBAC tối thiểu, **chỉ là proposal, chưa apply** |
| `tests/ci/test_agent_staging.py` (mới), `test_release_policy.py`, `test_inert_proposals.py` | Test offline; bất biến namespace của rehearsal được làm chặt hơn |

**Không đổi:**
- Pipeline groovy: đã có sẵn `DEPLOY_APPROVED`, bước input approval, kiểm tra release branch, build, Trivy và `deploy_release.sh --execute`.
- Release policy: rehearsal đã bắt buộc request rõ ràng, owner approval, branch `release/<group>` và image theo digest.
- Option B+ và các luồng production.

## 3. Jenkins deploy staging như thế nào

```
release/agent-staging @ SHA ─► job edai2-cd-agent-staging (DEPLOY_APPROVED=true + Approve release)
  CI (component tests) ─► build/reuse image (Cloud Build, digest) ─► Trivy ─► release_policy build (plan)
  ─► deploy_release.sh ─► release_dispatch (lock "agent-staging") ─► scripts.ci.agent_staging:
     preflight ─► helm template (từ chối object nằm ngoài staging) ─► helm install llm-agent-staging --wait
     ─► tạo WorkerPool + SA + SandboxAgents (create-only, nhãn execution) ─► chờ Coordinator/WorkerPool/
        SandboxAgents/golden actor ─► verify ─► cleanup (luôn chạy) ─► staging-verdict.json
```

Thứ tự lấy từ phụ thuộc thật trong repo:
1. MCP và Gateway trước, cùng release Helm.
2. Coordinator: kagent tạo Deployment từ `Agent`. Nó chỉ gọi Specialist khi có request.
3. WorkerPool, rồi SandboxAgents.
4. E2E.

Coordinator không cần Specialist để khởi động, nên không có bước nào chạy song song.

Branch push thông thường không bao giờ deploy staging: phải đi qua group `release/agent-staging`, có `DEPLOY_APPROVED`, bước approval, và SHA khớp `EXPECTED_GIT_SHA`.

## 4. Thiết kế WorkerPool và SandboxAgent

**Giống production:**
- `ateomImage`: ảnh ateom đã vá, cùng digest với production (egress proxy re-originate từ worker pod).
- `gvisor`, resources 500m/1Gi.
- Actor contract giữ nguyên: cmd, module, port 80, PATH/PYTHONPATH, proxy 169.254.17.1:3128, security context.

**Chỉ khác ở staging:**
- `replicas: 1`; không có scaleSelector, KEDA hay PDB.
- namespace và `workerPoolRef: staging-agents` (cùng namespace, vì kagent 0.10.3 không có trường namespace trong ref).
- `EWALLET_GATEWAY_NAMESPACE=ewallet-agents-staging`, `EWALLET_RUNTIME_NAMESPACE=ewallet-sandbox-staging`.
- Secret `mcp-token-sandbox-<subject>-<server>-staging`.
- Snapshot ở `gs://ate-snapshots/ewallet-sandbox-staging/<execution>/<agent>/`: đây là RustFS trong cụm, cú pháp `gs://` là của Substrate chứ không phải GCS.
- Nhãn `ewallet.io/cicd-execution` (bằng chứng ownership khi dọn) và `ewallet.io/release-sha`.
- Telemetry tắt.

Worker được admit vào MCP Gateway staging bằng NetworkPolicy và AuthorizationPolicy render trong `ewallet-agents-staging`, cho principal `cluster.local/ns/ewallet-sandbox-staging/sa/default` và pod có nhãn `ate.dev/worker-pool=staging-agents` (nhãn mà `kagent-pilot-worker-egress` và scaleSelector production đều dùng).

**Kiểm tra an toàn trong executor:**
- Contract chỉ được chứa namespace staging; mọi Secret phải là `-staging`.
- Image phải là digest, built từ SHA (`sandbox_agents.reconcile` đối chiếu manifest).
- Thiếu dependency staging thì `MISSING_PREREQUISITE`, **không fallback production**.
- Phát hiện tài nguyên của lần chạy trước (release Helm hoặc object fleet) thì `STALE_STAGING`, không adopt.
- Render có object nằm ngoài staging thì `RENDER_UNSAFE`.
- Cleanup chỉ xóa object cùng uid và cùng nhãn execution, và chỉ uninstall release do chính lần chạy này cài. Object không chứng minh được là của mình thì để lại và báo `CLEANUP_INCOMPLETE`.

## 5. Kiểm chứng (dùng lại test có sẵn)

| Check | Module dùng lại | Đích staging |
|---|---|---|
| Specialist Feature-RAG và Drift (known answers) | `specialist_e2e.verify` | `/api/a2a-sandboxes/ewallet-sandbox-staging/<agent>/` qua port-forward kagent |
| Coordinator fraud, drift, combined | `agi_probe` (request lấy từ fixture), `specialist_e2e.run_task`, `replay_verdict.e2e` | `/api/a2a/ewallet-agents-staging/fraud-coordinator/` qua A2A Gateway staging |
| A2A route không khai báo, và host production | `agi_probe.a2a_route` (yêu cầu 404, route control phải 200) | `payments-agent.ewallet-agents-staging`, `fraud-coordinator.ewallet-agents` |
| MCP ẩn danh bị từ chối | probe `initialize` không token | `/feature-rag/mcp`, `/drift/mcp` của Gateway staging |
| Token sai, tool không được cấp, staging trỏ sang production | `test_mcp_production_authenticator`, `test_c5_mcp_failover`, `test_v2p1_gateway_environment` | offline (CD không đọc giá trị Secret) |

Khi PASS, verdict ghi hai kết quả tách biệt: **`AGENT_E2E_VERIFIED`** và **`MANAGED_PROMPT_RUNTIME_NOT_YET_VERIFIED`**. NL intake trong staging vẫn tắt.

## 6. Test offline đã chạy

| Lệnh (uv env khóa) | Kết quả |
|---|---|
| `pytest tests/ci/test_agent_staging.py` (tooling312) | **15 passed**, 17 subtests |
| `pytest tests/ci` (tooling312) | 646 passed, 4 skipped |
| `tests/ci` cùng chart/staging/prompt/AGI/sandbox experiment, với `EWALLET_RENDER_BASELINE_REF=0fff5625` | 795 passed |
| coord312: `test_controller_stdlib`, `test_catalog`, gateway, completion, sandbox metrics | 57 passed; 1 fail `test_release_policy_and_handoff_run_without_pyyaml` đã có sẵn ở baseline coord312 (pass ở tooling312) |
| `render_equivalence.py compare` (8 profile production) | `PRODUCTION_RENDER_BYTE_IDENTICAL` |
| `helm lint` | 0 failed. Staging render: 55 object, toàn bộ trong `ewallet-agents-staging`, không vi phạm isolation |

`test_agent_staging.py` kiểm:
- chọn unit staging, và từ chối khi thiếu request/approval, sai branch, thiếu digest hoặc dùng tag;
- unit nhắm `ewallet-agents`, `ewallet-sandbox` hoặc `ewallet-cicd` đều bị từ chối;
- lệnh dispatch;
- contract từ chối namespace production và Secret dùng chung;
- fleet hợp lệ theo CRD kagent 0.10.3 và giữ đúng actor contract của production;
- executor trên fake Kubernetes và fake Helm: thứ tự bước, cleanup đảo ngược (SandboxAgent trước pool), cleanup vẫn chạy khi E2E fail, các lần từ chối không gây mutation nào, render không an toàn, object không phải của mình được để nguyên;
- `verify()` chỉ nhắm vào staging, và thất bại khi MCP chấp nhận gọi ẩn danh hoặc Coordinator trả kết quả partial.

Mutation check: bỏ kiểm tra ownership khi cleanup, hoặc bỏ kiểm tra render, đều làm test fail.

## 7. Tương thích production

- Render production vẫn byte-identical; staging guard và isolation test vẫn PASS.
- Unit, group và lệnh production (Option B+, Helm promotion, fleet patch) không đổi.
- Các thay đổi trong `sandbox_agents` và `sandbox_candidate` chỉ thêm tham số có mặc định production; toàn bộ test production đều pass.

## 8. Điều kiện cloud còn lại (`PENDING_RUNTIME_VERIFICATION`)

1. **Watch scope kagent:** cập nhật overlay như mục 1. Controller dùng chung sẽ restart; cần owner duyệt.
2. **WorkerPool:** pool thứ hai dùng ateom đã vá cùng chạy với `business-agents`; worker nhận identity `ns/ewallet-sandbox-staging/sa/default` qua ambient; nhãn `ate.dev/worker-pool` có trên worker pod.
3. **Dung lượng RustFS:** chưa biết dung lượng trống thực tế (256Mi trong values pilot không chứng minh gì). Check chỉ đọc trước khi chạy: `kubectl -n ate-system get pvc,statefulset -l app.kubernetes.io/name=rustfs` và dung lượng đã dùng của bucket `ate-snapshots` (ví dụ `kubectl -n ate-system exec <rustfs-pod> -- du -sh /data`). Phải đủ ít nhất 2 lần kích thước golden snapshot của hai Specialist.
4. **Credential (tạo ngoài Helm, thu hồi sau run, không tái dùng production):**

   | Namespace | Secret | Nguồn và chủ sở hữu |
   |---|---|---|
   | `ewallet-sandbox-staging` | `mcp-token-sandbox-feature-rag-feature-rag-mcp-staging`, `mcp-token-sandbox-drift-drift-mcp-staging` | token client MCP (`ops/llm_agent/g3_mcp_credentials.py`, release `staging`); owner |
   | `ewallet-agents-staging` | `mcp-auth-feature-rag-mcp-staging`, `mcp-auth-drift-mcp-staging` | bảng token server (subject `sandbox-feature-rag`, `sandbox-drift`, `kagent-controller`); owner |
   | `ewallet-agents-staging` | `mcp-token-kagent-controller-{feature-rag,drift}-mcp-staging` | token list-only cho discovery của kagent; owner |
   | `ewallet-agents-staging` | `coordinator-idempotency-redis-auth-staging` | `connection_url`, `password`, `users.acl` của Redis staging; owner |
   | `ewallet-agents-staging` | `feast-online-redis-staging-fraud`, `feast-online-redis-staging-rag` | connection tới Feast Redis staging; owner |
   | `ewallet-agents-staging` | `agentgateway-client-key-staging` | chỉ là tham chiếu của ModelConfig (NL tắt); owner |

5. **Dữ liệu downstream (blocker chính cho E2E).** Known answers chỉ đúng khi staging phục vụ đúng các fixture row và cùng model. Mức tối thiểu khả thi:
   - Feast Online Redis staging (DB fraud `1`, rag `4`) chỉ chứa entity của fixture `agent-load-request.json`, materialize từ offline snapshot bằng `ai_platform/features/feast/online_operator.py` (registry `feast-registry-fraud-staging` / `-rag-staging`);
   - KServe predictor `fraud-detector-predictor.ewallet-agents-staging` dùng cùng model artifact `fraud-detector/1`, kèm network flow từ `feature-rag-mcp` tới Redis và predictor (khai báo sau khi có identity);
   - ConfigMap `drift-baselines-staging` / `drift-window-staging` là bản sao của artifact drift (executor tự tính SHA và `lineage.snapshot_id`).

   Không dùng Redis hay transaction production, và không thay known answers bằng kết quả giả. Owner cần quyết định có được đọc offline snapshot (chỉ đọc) để seed hay không.
6. **RBAC Jenkins:** `agent-staging-cd.proposed.yaml`: Role theo namespace, không có ClusterRole, có port-forward tới kagent. Owner duyệt rồi apply.
7. **Managed prompt (Row 34):** test riêng, sau Level A: publish prompt → `prompt_registry.py configmap` → bật `managedPrompt` + NL intake → LLM Gateway và backend thật. Không chạy trong Level A.

## 9. Runbook V2-P3B (chỉ thực hiện khi owner duyệt)

**Preflight**
1. Có owner approval cho: wake (resize-only), nâng watch scope kagent, RBAC staging, seed dữ liệu staging, chi phí.
2. Ghi lại `EXPECTED_GIT_SHA` (HEAD của `release/agent-staging`) và digest image (từ build hoặc reuse).
3. Wake GKE resize-only; kagent controller và `ate-system` Ready; ghi trạng thái production (Helm revisions, SandboxAgent production Ready).
4. Apply `infra/kubernetes/agent-staging/namespaces.yaml`; nâng overlay watch scope; xác nhận SandboxAgent production vẫn Ready.
5. Tạo các Secret ở mục 8.4 và các ConfigMap registry/drift staging; seed dữ liệu theo mục 8.5.
6. Apply RBAC proposal đã duyệt. Đo dung lượng RustFS (mục 8.3). Kiểm tra NetworkPolicy/ambient của hai namespace.

**Deploy và verify**
- Push `release/agent-staging` tại SHA.
- Chạy job `edai2-cd-agent-staging` với `EXPECTED_GIT_SHA`, `RELEASE_BRANCH=release/agent-staging`, `DEPLOY_APPROVED=true`, rồi bấm Approve.
- Pipeline tự chạy: CI → build/scan → `agent-staging` (deploy → ready → Specialist E2E → Coordinator fraud/drift/combined → negatives → cleanup).
- Test managed prompt (tùy chọn) chạy riêng, sau Level A.

**Evidence (owner tự chụp):**
- Jenkins Stage View và Console Output;
- kết quả build và Trivy;
- `kubectl get` của hai namespace staging, WorkerPool và SandboxAgent Ready (chụp trong lúc job chạy);
- `staging-verdict.json` (E2E, negatives);
- Jenkins SUCCESS.

**Cleanup**
1. Executor tự xóa object của mình và uninstall release. Kiểm tra lại: `kubectl get all,workerpools,sandboxagents -n ewallet-*-staging` phải trống, và `helm list -n ewallet-agents-staging` phải trống.
2. Xóa snapshot dưới prefix `ewallet-sandbox-staging/<execution>/`; thu hồi các token staging.
3. Tùy owner: trả watch scope về như cũ; xóa namespace staging.
4. Sleep GKE theo quy trình đã duyệt. Không dùng guarded sleep nếu chưa có approval.
