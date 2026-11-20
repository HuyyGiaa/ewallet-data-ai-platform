# CI/CD V2-P3B-0 — Giải quyết điều kiện tiên quyết của staging (offline)

**Verdict: `V2_P3B0_OFFLINE_READY`**

- `STAGING_DATA_OFFLINE_PREPARED`
- `KAGENT_WATCH_SCOPE_OFFLINE_PREPARED` (restart controller: `PENDING_RUNTIME_VERIFICATION`)
- `STAGING_CREDENTIALS_INVENTORIED`
- `STAGING_RBAC_OFFLINE_VALIDATED`
- `RUSTFS_CAPACITY_PREFLIGHT_PREPARED`

Phase này chỉ làm offline:
- Không đánh thức GKE, không kết nối cluster.
- Không chạy Jenkins, không tạo Secret, không apply manifest.
- Không nâng cấp kagent hay Substrate.

Mọi thao tác cloud nằm ở mục G và cần owner duyệt.

---

## A. Dữ liệu staging

### A.1 Ma trận dependency theo case E2E

| Dependency | Fraud | Drift | Combined | Nguồn | Cách chuẩn bị | Trạng thái |
|---|---|---|---|---|---|---|
| Feast Redis staging `feast-staging-redis` | ✔ | – | ✔ | manifest mới `infra/kubernetes/agent-staging/feast-redis.yaml` | operator apply một lần (mục G.5 bước 5) | PREPARED |
| Fraud online rows (DB 1) | ✔ | – | ✔ | Feature Store Redis production, DB 0, project `ewallet_fraud_online`: 4 key entity của fixture | `staging_feature_seed.py copy fraud`: đọc DUMP từ production, RESTORE sang staging, so digest | PREPARED; cần **owner duyệt đọc production** |
| RAG online rows (DB 4) | ✔ | – | ✔ | Feature Store Redis production, DB 3, project `ewallet_rag_hybrid_20261114t144825` (DB verification, ≤ 64 key) | `staging_feature_seed.py copy rag` | PREPARED; cần **owner duyệt đọc production** |
| Feast registry `feast-registry-fraud-staging` | ✔ | – | ✔ | ConfigMap live `ewallet-mcp/feast-registry-fraud-online` (sha256 ghi nhận `0f578d53a62f0f447fd693153aa860735ddaf9e8cad5bda388be7acabb685960`, 13547 byte) | copy ConfigMap, kiểm tra sha (G.1 bước 7, G.5 bước 3) | PREPARED; nguồn chỉ có trên cluster |
| Feast registry `feast-registry-rag-staging` | ✔ | – | ✔ | ConfigMap live `ewallet-mcp/feast-registry-rag-agi` (sha256 ghi nhận `2ca6829930f96f2189b929abe877f0149eb5335f17a38fb89f10d1500b715930`, 2275 byte) | như trên | PREPARED; nguồn chỉ có trên cluster |
| KServe predictor `fraud-detector-predictor` | ✔ | – | ✔ | cùng image digest với production (`fraud-detector-predictor@sha256:cba90c22…b896`) | manifest mới `infra/kubernetes/agent-staging/fraud-predictor.yaml` | PREPARED |
| `drift-baselines-staging` (`baselines.json`) | – | ✔ | ✔ | artifact local `agi-20261115T145608Z/inputs/drift/baselines.json`, sha256 `d6f87cc7…e473` | `agi_inputs drift-configmaps --suffix staging` | PREPARED (không cần đọc production) |
| `drift-window-staging` (`window-manifest.json` + `account_behavior.csv.gz`) | – | ✔ | ✔ | artifact local, manifest sha256 `3215b11e…a2c2`, csv sha256 `fbad1a8a…2aa0`, `dataset_revision` `feast-r5gb-20261111-214544` | như trên | PREPARED |
| RAG corpus/manifest | ✔ | – | ✔ | nằm trong image MCP (`/workspace/data/llm_agent/rag/fraud-policy-v1/manifest.json`) | không cần làm gì | SOURCE_VERIFIED |

Không có dependency nào ở trạng thái `SOURCE_NOT_AVAILABLE`. Hai registry ConfigMap chỉ có bản trên cluster, chưa có bản local cùng sha. Vì vậy ở G.1 bước 7 và G.5 bước 3, sha live phải khớp giá trị đã ghi nhận ở trên. Nếu sha khác thì dừng, không tự chọn nguồn khác.

### A.2 Vì sao copy đúng byte thay vì materialize lại

- Known answers của staging lấy từ sandbox candidate, nên chỉ đúng với **chính các row online** mà production đã đọc.
- Nếu materialize lại từ offline snapshot, window và timestamp sẽ khác, nên giá trị feature khác và known answers không còn đúng.

Vì vậy `ops/llm_agent/staging_feature_seed.py` làm như sau:

- **Fraud:** key được tính bằng hàm `_redis_key` của chính Feast (project, entity key, serialization v3), chỉ cho 4 entity của fixture `agent-load-request.json`. Thiếu key nào là lỗi, không bỏ qua. Đã chạy offline với feast 0.66 và ra 4 key khác nhau.
- **RAG:** copy toàn bộ DB verification, tối đa 64 key. Owner marker của Feature Store phải ghi đúng project RAG.
- **Ràng buộc fail-closed, kiểm tra trước khi ghi:**
  - identity được kiểm tra trên chính connection string, trước mọi kết nối: source phải là `redis.feature-store.svc.cluster.local:6379` với DB 0 (fraud) hoặc DB 3 (RAG), target phải là `feast-staging-redis.ewallet-agents-staging.svc.cluster.local:6379` với DB 1 (fraud) hoặc DB 4 (RAG). Host khác (kể cả `127.0.0.1`), port khác hay DB khác đều bị từ chối;
  - khi chạy từ máy trạm (`--context CTX`), script tự mở port-forward tới đúng 2 Service cố định và chỉ kết nối qua port đó (xem H.1); không chấp nhận port-forward do operator tự mở;
  - source và target phải là hai Redis server khác nhau (`INFO` run_id), và DB target phải rỗng;
  - `RESTORE` không dùng `REPLACE`;
  - sau khi ghi, digest SHA-256 của source phải bằng của target. Digest tính trên **nội dung logic** (`canonical-v1`: TYPE, PTTL phải là -1, giá trị string hoặc mọi cặp field/value của hash sắp theo field, mỗi phần có tiền tố độ dài), không tính trên payload `DUMP`.
    Lý do (G.5.6): hash có giá trị dài hơn `hash-max-listpack-value` (các row embedding RAG) dùng encoding `hashtable`, và `DUMP` ghi field theo thứ tự duyệt dict, phụ thuộc hash seed riêng của từng server. Cùng một hash, sau `RESTORE`, cho payload khác dù nội dung y hệt. Vì vậy digest trên `DUMP` báo sai "khác" cho RAG, còn fraud (listpack) thì không. `DUMP`/`RESTORE` vẫn là cách chuyển dữ liệu.
- **Production chỉ bị đọc:** chỉ dùng lệnh `DUMP`, `SCAN`, `GET`, `TYPE`, `PTTL`, `HGETALL`.
- **Không lộ connection string:** lấy từ biến môi trường, không bao giờ in ra hay ghi xuống đĩa.
- **Không động đến model và known answers:** không retrain model, không đổi known answers, không copy cả database production.

`values-staging.yaml` đổi `EWALLET_RAG_FEAST_PROJECT` từ `ewallet_rag_staging` sang `ewallet_rag_hybrid_20261114t144825`, vì project này phải trùng với project của các row được seed. Profile `isolated` trong `service.py` vẫn chấp nhận: DB 4 nằm trong dải verification 2–15, project có dạng `ewallet_rag_<suffix>`, và DB fraud khác DB RAG.

### A.3 Mạng và quota

- **Network flow:** `values-staging.yaml` khai báo hai workload không do chart sở hữu (`feast-staging-redis`, `fraud-detector`) cùng hai enforced flow:
  - `feature-rag-mcp → feast-staging-redis:6379`
  - `feature-rag-mcp → fraud-detector:8080`

  Mỗi flow render thành AuthorizationPolicy và cặp NetworkPolicy, tất cả nằm trong `ewallet-agents-staging`.
- **Probe:** predictor có NetworkPolicy riêng cho HTTP probe đi từ `169.254.7.127/32`. Redis dùng exec probe nên không cần.
- **Quota Service:** tăng từ 8 lên 10 (`namespaces.yaml`). Chart có 5 Service, cộng thêm Service của kagent Agent, Redis và predictor là đúng 8 mà không còn biên.
- **Quota CPU/RAM:** tổng request khoảng 0,9 CPU và 2,2Gi, nằm trong quota 2 CPU / 4Gi.
- **Rủi ro Redis staging:** Redis staging không có persistence. Nếu pod restart thì dữ liệu mất và E2E sẽ fail (không fail âm thầm). Khi đó chạy `verify`, rồi seed lại.

### A.4 Thay đổi để dùng các prerequisite

- **Contract:**
  - `required` là đúng 10 Secret Level A;
  - thêm `dependencies.deployments`: `feast-staging-redis` và `fraud-detector-predictor`, mỗi cái có image pin bằng digest;
  - thêm `llm_only_secrets`.
- **`agent_staging.py`:**
  - `load_contract` từ chối dependency không pin digest;
  - preflight yêu cầu hai Deployment ở trạng thái Ready và chạy đúng image đã pin;
  - Helm chạy với `HELM_DRIVER=configmap`.
- **Lỗi V2-P3A đã sửa:** preflight cũ đọc `lineage.snapshot_id` từ window manifest, trong khi trường đó chỉ có ở *export* manifest đầu vào. Window manifest thật (`ewallet-drift-window/v1`) có `dataset_revision` ở cấp gốc. Với artifact thật, preflight cũ sẽ luôn báo `MISSING_PREREQUISITE`.

## B. Watch scope của kagent (PKG-KS)

| | Watch namespaces |
|---|---|
| Cũ (sau PKG-K, đang live) | `ewallet-agents,kagent-pilot,ewallet-sandbox` |
| Mới | `ewallet-agents,kagent-pilot,ewallet-sandbox,ewallet-agents-staging,ewallet-sandbox-staging` |

Các file thay đổi:

- **Overlay** `infra/kubernetes/kagent-pilot/values/kagent-staging-watch-overlay.yaml`: giữ nguyên mọi namespace hiện có; `rbac.namespaces` thêm 2 namespace staging.
- **`sandbox_watch.sh`:** thêm target `kagent-staging`, package `PKG-KS`. Luồng giữ nguyên như PKG-K:

  > preflight identity → exact diff → server dry-run → upgrade có approval → verify → rollback về revision đã ghi lúc upgrade

  Revision không được hardcode. Verify kiểm tra:
  - RBAC đã có ở cả 2 namespace staging;
  - log controller có liệt kê đủ các namespace;
  - SandboxAgent production ở `ewallet-sandbox` vẫn Ready.
- **`sandbox_watch_diff.py`:** diff phải khớp chính xác:
  - added: Role và RoleBinding writer/getter ở 2 namespace staging, cộng cặp env-source **chỉ** ở `ewallet-sandbox-staging`;
  - changed: `WATCH_NAMESPACES` và checksum của Deployment.

  Object nào nằm ngoài danh sách thì bị từ chối.
- **Post-renderer:** env-source RBAC chỉ giữ lại ở namespace sandbox (`ewallet-sandbox`, `ewallet-sandbox-staging`), bị bỏ ở `ewallet-agents-staging`. Cặp tùy chọn phải đủ cả hai. Render production (baseline) vẫn byte-identical.
- **Rollback:** `WATCH_APPROVAL=PKG-KS-ROLLBACK:<ctx> sandbox_watch.sh kagent-staging rollback <dir>` đưa release về revision đã ghi, rồi kiểm tra identity theo revision đó.

Controller dùng chung sẽ restart một lần khi upgrade. Restart này chưa được kiểm chứng (`PENDING_RUNTIME_VERIFICATION`).

## C. Credential, RBAC, RustFS

### C.1 Inventory Secret chính thức

Con số "12" trong báo cáo V2-P3A là sai. Bản render có 10 tham chiếu Secret.

Inventory Level A cũng có 10 Secret: 9 Secret được render, bỏ 1 Secret chỉ dùng cho LLM, cộng thêm `feast-staging-redis-auth` của Redis staging.

Nguồn duy nhất là `SECRETS` trong `ops/llm_agent/staging_credentials.py`. Test kiểm tra contract khớp với nguồn này.

| # | Namespace | Secret | Key | Consumer | Nhóm |
|---|---|---|---|---|---|
| 1 | agents-staging | `mcp-auth-feature-rag-mcp-staging` | `auth-config.json` | Deployment/feature-rag-mcp | Level A |
| 2 | agents-staging | `mcp-token-kagent-controller-feature-rag-mcp-staging` | `token` | RemoteMCPServer (discovery của kagent) | Level A |
| 3 | sandbox-staging | `mcp-token-sandbox-feature-rag-feature-rag-mcp-staging` | `token` | SandboxAgent/feature-rag-specialist | Level A |
| 4 | agents-staging | `mcp-auth-drift-mcp-staging` | `auth-config.json` | Deployment/drift-mcp | Level A |
| 5 | agents-staging | `mcp-token-kagent-controller-drift-mcp-staging` | `token` | RemoteMCPServer | Level A |
| 6 | sandbox-staging | `mcp-token-sandbox-drift-drift-mcp-staging` | `token` | SandboxAgent/drift-specialist | Level A |
| 7 | agents-staging | `coordinator-idempotency-redis-auth-staging` | `password`, `connection_url`, `users.acl` | StatefulSet idempotency Redis, Agent/fraud-coordinator | Level A |
| 8 | agents-staging | `feast-staging-redis-auth` | `password` | Deployment/feast-staging-redis | Level A |
| 9 | agents-staging | `feast-online-redis-staging-fraud` | `connection_string` (`…,db=1`) | Deployment/feature-rag-mcp | Level A |
| 10 | agents-staging | `feast-online-redis-staging-rag` | `connection_string` (`…,db=4`) | Deployment/feature-rag-mcp | Level A |
| – | agents-staging | `agentgateway-client-key-staging` | `api-key` | ModelConfig/ewallet-staging-llm | chỉ LLM (managed prompt / NL, không thuộc Level A) |
| – | – | (sau này) token client cho natural-language intake, Langfuse | – | – | future, chưa tạo |

**Tạo Secret:** `staging_credentials.py create --context CTX --approve`.

- Mọi giá trị được sinh trong bộ nhớ, tạo Secret immutable, không in giá trị ra.
- Từ chối chạy nếu đã có bất kỳ Secret nào trong danh sách: không ghi đè, không merge một phần.
- Tên Secret và format bảng token dùng lại `g3_mcp_credentials` (`token_secret_name`, `auth_secret_name`, version 1).
- Test parse bảng bằng chính `ServiceTokenAuthenticator` và xác nhận mỗi token map đúng subject.

**Thu hồi:** `revoke --context CTX --approve` xóa đúng các tên trên.

Ghi chú: Secret Redis có cùng quyền với `requirepass` (không có ACL). Riêng idempotency Redis có ACL giới hạn theo prefix key.

### C.2 RBAC của Jenkins (`system:serviceaccount:ewallet-ci:jenkins`)

Đề xuất nằm ở `infra/kubernetes/jenkins/rbac/agent-staging-cd.proposed.yaml`. File chưa được duyệt, chưa apply. Kết quả audit so với thao tác thật:

- **Helm:** dùng `HELM_DRIVER=configmap`, nên release record là ConfigMap. Jenkins **không ghi Secret** nào. Chart staging cũng không render Secret.
- **Secret: chỉ `get`/`list`**, ở cả 2 namespace staging. Code chỉ hỏi metadata (`METADATA_ONLY`), nhưng RBAC không phân biệt được: **`get`/`list` vẫn cho phép đọc giá trị** của mọi Secret trong namespace đó. Chấp nhận được vì 2 namespace này chỉ chứa credential staging. Mô tả "metadata-only" cũ đã được sửa.
- **Phạm vi:**
  - chỉ Role theo namespace (`ewallet-agents-staging`, `ewallet-sandbox-staging`, `kagent`), không có ClusterRole;
  - subject duy nhất là `ewallet-ci/jenkins`;
  - không có quyền trên `namespaces`, không dùng wildcard;
  - ở `kagent` chỉ đọc, cộng `pods/portforward create`.
- **Test offline** `tests/ci/test_agent_staging_rbac.py` kiểm tra:
  - mọi thao tác (Helm trên mọi kind được render, kiểm tra Secret, readiness của dependency, port-forward, WorkerPool/SandboxAgent/ServiceAccount, ActorTemplate) đều được cho phép;
  - kind trong bản render ⊆ kind mà RBAC phủ.

**Kiểm tra live** (V2-P3B, chỉ đọc). Mọi dòng phải trả `yes`, trừ 4 dòng cuối phải trả `no`:

```bash
J=system:serviceaccount:ewallet-ci:jenkins; A=ewallet-agents-staging; S=ewallet-sandbox-staging
for r in configmaps services serviceaccounts deployments.apps statefulsets.apps networkpolicies.networking.k8s.io \
         authorizationpolicies.security.istio.io agents.kagent.dev modelconfigs.kagent.dev remotemcpservers.kagent.dev; do
  for v in create get list patch update delete; do echo "$A $v $r $(kubectl --context $CTX auth can-i $v $r -n $A --as=$J)"; done; done
for ns in $A $S; do kubectl --context $CTX auth can-i get secrets -n $ns --as=$J; kubectl --context $CTX auth can-i list secrets -n $ns --as=$J; done
kubectl --context $CTX auth can-i create pods/portforward -n $A --as=$J
for r in workerpools.ate.dev sandboxagents.kagent.dev serviceaccounts; do for v in create get delete; do kubectl --context $CTX auth can-i $v $r -n $S --as=$J; done; done
kubectl --context $CTX auth can-i list actortemplates.ate.dev -n $S --as=$J
kubectl --context $CTX auth can-i get services -n kagent --as=$J; kubectl --context $CTX auth can-i create pods/portforward -n kagent --as=$J
# must be "no":
kubectl --context $CTX auth can-i create secrets -n $A --as=$J
kubectl --context $CTX auth can-i get secrets -n ewallet-agents --as=$J
kubectl --context $CTX auth can-i create namespaces --as=$J
kubectl --context $CTX auth can-i patch deployments -n kagent --as=$J
```

### C.3 Dung lượng RustFS (chỉ đọc, không resize)

Giá trị 256Mi trong values pilot không chứng minh gì về dung lượng thật. Phải đo trên cluster:

```bash
kubectl --context $CTX -n ate-system get pvc -o custom-columns=NAME:.metadata.name,CAP:.status.capacity.storage,CLASS:.spec.storageClassName
kubectl --context $CTX -n ate-system get pods -o name | grep rustfs          # pick the RustFS pod
POD=<pod>; kubectl --context $CTX -n ate-system get $POD -o jsonpath='{range .spec.containers[*].volumeMounts[*]}{.mountPath}{"\n"}{end}'
D=<data mount>
kubectl --context $CTX -n ate-system exec $POD -- df -k "$D"                                  # F = Available (KiB)
kubectl --context $CTX -n ate-system exec $POD -- du -sk "$D/ate-snapshots/ewallet-sandbox"   # S = golden snapshots of the 2 production Specialists
kubectl --context $CTX -n ate-system exec $POD -- du -sk "$D/ate-snapshots/ewallet-sandbox-staging" 2>/dev/null   # leftovers: must be absent/0
```

**Quy tắc GO/NO-GO:**

- **GO** khi đủ cả hai điều kiện:
  - đo được S và S > 0;
  - F ≥ 2·S + 65536 KiB. Hệ số 2 dành cho một bộ snapshot staging cộng một lần restore dự phòng; 64Mi là biên an toàn.
- **NO-GO** nếu S hoặc F không đo được (ví dụ image không có `df`/`du`), nếu F không đủ, hoặc nếu còn sót prefix staging từ lần trước.

Khi NO-GO thì không resize và không xóa snapshot production, chỉ báo cho owner. `exec` vào `ate-system` là quyền của owner, Jenkins không có quyền này.

Việc Substrate có tự xóa snapshot khi SandboxAgent bị xóa hay không vẫn chưa kiểm chứng (`PENDING_RUNTIME_VERIFICATION`). Vì vậy phải đo lại prefix staging sau khi chạy.

## D. File thay đổi

| File | Thay đổi |
|---|---|
| `infra/kubernetes/agent-staging/feast-redis.yaml`, `fraud-predictor.yaml` (mới) | Redis staging và KServe predictor staging, pin digest, chỉ ở namespace staging |
| `infra/kubernetes/agent-staging/namespaces.yaml` | quota Service 8 → 10 |
| `ops/llm_agent/staging_feature_seed.py` (mới) | seed bằng DUMP/RESTORE có giới hạn, kiểm tra digest |
| `ops/llm_agent/staging_credentials.py` (mới) | inventory Level A, tạo và thu hồi Secret |
| `infra/helm/llm-agent-platform/values-staging.yaml` | project RAG, 2 workload, 2 network flow |
| `jenkins/cd/agent-staging.contract.json` | 10 Secret, `dependencies`, `llm_only_secrets` |
| `scripts/ci/agent_staging.py` | preflight dependency, `HELM_DRIVER=configmap`, sửa `dataset_revision` |
| `infra/kubernetes/jenkins/rbac/agent-staging-cd.proposed.yaml` | Secret chỉ get/list, mô tả đúng về mức lộ dữ liệu |
| `scripts/llm_agent/sandbox_watch.sh`, `ops/llm_agent/sandbox_watch_diff.py`, `kagent_runtime_post_renderer.py`, overlay mới | PKG-KS |
| tests | `test_agent_staging.py` (dependency, helm driver, inventory), `test_agent_staging_rbac.py` (mới), `test_staging_prerequisites.py` (mới), `test_sandbox_watch_script.py`, `test_agent_platform_packages.py`, `test_kagent_bounded_pilot.py` |

## E. Kiểm chứng offline

| Bộ test | Kết quả |
|---|---|
| tooling312: `tests/ci`, `test_v2p1_staging_chart` (`EWALLET_RENDER_BASELINE_REF=0fff5625`), `test_v2p2_managed_prompt`, `test_sandbox_watch_script`, `test_agent_platform_packages`, `test_staging_prerequisites`, `test_kagent_bounded_pilot` | 718 passed, 5 skipped |
| Các test bị skip | 4 test cần môi trường g3/feast (đã skip từ trước); 1 test fraud-key cần feast, đã chạy riêng bằng `ai_platform/inference/.venv` và ra 4 key |
| `render_equivalence compare` so với baseline V2-P1 | `PRODUCTION_RENDER_BYTE_IDENTICAL` (5 profile) |
| `helm lint` cho profile staging | 0 lỗi |

## F. Rủi ro còn lại (`PENDING_RUNTIME_VERIFICATION`)

1. Restart của kagent controller dùng chung khi nâng watch scope.
2. KServe Standard mode tạo đúng `fraud-detector-predictor` và label `serving.kserve.io/inferenceservice` trong namespace staging. Image pull từ Artifact Registry chạy được trong namespace mới.
3. Sha của 2 registry ConfigMap live vẫn khớp giá trị đã ghi nhận.
4. Dung lượng RustFS, và Substrate có tự dọn snapshot sau run hay không.
5. PVC của idempotency Redis (volumeClaimTemplate) không bị `helm uninstall` xóa. Sau mỗi run, owner xóa PVC đó, hoặc chấp nhận dùng lại (request id mỗi run là duy nhất).

## G. Runbook V2-P3B

`CTX` là context GKE, `W` là thư mục làm việc của campaign. Mọi lệnh chạy trên nhánh `release/agent-staging` tại `EXPECTED_GIT_SHA`. Nhánh này do owner tạo; phase này không tạo.

Thứ tự 5 giai đoạn bám theo điều kiện thật của script: `sandbox_watch.sh kagent-staging preflight` dừng với `namespace … does not exist (create it first)` nếu 2 namespace staging chưa có. Vì vậy namespace phải được tạo (giai đoạn 2) trước khi chạy preflight/diff/dry-run của PKG-KS (giai đoạn 3).

### G.0 Chuẩn bị Jenkins (trước giai đoạn 6, mỗi bước publish/apply cần owner duyệt)

Jenkins không đọc GitHub. Job check out `/opt/ewallet-source/repository.bundle` (image `jenkins-source-snapshot`), nên push `release/agent-staging` lên GitHub không thay đổi gì với Jenkins ([`source_snapshot.py`](../../../scripts/ci/source_snapshot.py)). Jenkins live (V2-P3B preflight 2026-11-20) vẫn chạy snapshot `d84ea4e9`, CasC chưa có job `edai2-cd-agent-staging`, và image controller cũ không có `helm` mà `agent_staging.py` cần. Vì vậy phải publish 2 image mới và redeploy controller.

Hai image trước đây được build trên workstation bằng `docker buildx … --push`, tag là full SHA (repository `ewallet-platform` bật immutable tags):

```bash
AR=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
SHA=<release SHA>; BASE=98b71e30e51914f25ad17974debffadbdaca2487; W=<campaign dir>
# 1. Controller image (helm 3.22.0, checksum-verified in the Dockerfile)
docker buildx build --platform linux/amd64 -f infra/docker/jenkins-controller/Dockerfile -t $AR/jenkins-controller:$SHA --push .
# 2. Source snapshot: bundle offline, verify, then the image
python3 scripts/ci/source_snapshot.py build --sha $SHA --base $BASE --out $W/snapshot
python3 scripts/ci/source_snapshot.py verify --dir $W/snapshot --sha $SHA      # verdict PASS
docker buildx build --platform linux/amd64 -f infra/docker/jenkins-source-snapshot/Dockerfile -t $AR/jenkins-source-snapshot:$SHA --push $W/snapshot
# 3. Digests (the manifest pins digests, never tags)
for i in jenkins-controller jenkins-source-snapshot; do gcloud artifacts docker images describe $AR/$i:$SHA --format='value(image_summary.digest)'; done
# 4. Render; the PVC is left out (a PVC only grows; the live jenkins-home is 32Gi)
python3 scripts/ci/render_jenkins_runtime.py --controller-image $AR/jenkins-controller@<digest> \
  --source-image $AR/jenkins-source-snapshot@<digest> --source-sha $SHA --base-sha $BASE --ml-sha $SHA \
  --agent-sha $SHA --docs-sha $SHA --jenkins-gsa ewallet-jenkins@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com \
  --output $W/jenkins-rendered.yaml
python3 -c "import sys,yaml; print('---\n'.join(yaml.safe_dump(d, sort_keys=False) for d in yaml.safe_load_all(open(sys.argv[1])) if d and d['kind'] != 'PersistentVolumeClaim'))" \
  $W/jenkins-rendered.yaml > $W/jenkins-apply.yaml
grep -c "edai2-cd-agent-staging" $W/jenkins-apply.yaml                         # 1
# 5. After the wake: diff must change only ConfigMap jenkins-casc (one new job) and Deployment jenkins (SHAs, 2 images)
kubectl --context $CTX diff -f $W/jenkins-apply.yaml
kubectl --context $CTX apply --dry-run=server -f $W/jenkins-apply.yaml
kubectl --context $CTX apply -f $W/jenkins-apply.yaml && kubectl --context $CTX -n ewallet-ci rollout status deploy/jenkins --timeout=15m
```

Sau khi controller Ready: kiểm tra job `edai2-cd-agent-staging` có trong Jenkins; nếu job mới chưa hiện thì `Reload Configuration from Disk` (POST `/reload`), như Batch 2B-1. Kiểm tra `kubectl -n ewallet-ci exec deploy/jenkins -- helm version --short` = `v3.22.0+g144ca65`. Controller dùng `Recreate`, nên không được có build nào đang chạy khi apply.

### G.1 Giai đoạn 1 — Kiểm tra cluster chỉ đọc (sau khi owner duyệt wake)

Không có thao tác ghi nào.

1. `python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$M"`.
2. Ghi lại Helm revision và values hiện tại của `kagent` (namespace `kagent`), `llm-agent` (namespace `default`), `llm-agent-mcp` (namespace `ewallet-mcp`):
   ```bash
   helm --kube-context $CTX -n kagent history kagent --max 3; helm --kube-context $CTX -n kagent get values kagent -o yaml > $W/kagent-values-before.yaml
   ```
3. Watch scope hiện tại: `kubectl --context $CTX -n kagent get cm kagent-controller -o jsonpath='{.data.WATCH_NAMESPACES}'`. Kết quả phải là `ewallet-agents,kagent-pilot,ewallet-sandbox`.
4. Agent và SandboxAgent production đều Ready (`kubectl get agents -n ewallet-agents`, `get sandboxagents,workerpools -n ewallet-sandbox`). Controller `kagent`, `ate-system` và KServe Ready.
5. Tài nguyên còn trống của node (`kubectl describe nodes | grep -A6 'Allocated resources'`).
6. Đo RustFS và áp quy tắc GO/NO-GO (C.3).
7. Kiểm tra sha của 2 registry nguồn:
   ```bash
   kubectl -n ewallet-mcp get cm feast-registry-fraud-online -o jsonpath='{.binaryData.registry\.db}' | base64 -d | sha256sum
   ```
   Kết quả phải là `0f578d53a62f0f447fd693153aa860735ddaf9e8cad5bda388be7acabb685960`. Làm tương tự với `feast-registry-rag-agi`, kết quả phải là `2ca6829930f96f2189b929abe877f0149eb5335f17a38fb89f10d1500b715930`.
8. Offline: `python -m ops.llm_agent.staging_credentials plan`, `python -m ops.llm_agent.staging_feature_seed plan`.

### G.2 Giai đoạn 2 — Tạo namespace staging (owner duyệt)

```bash
kubectl --context $CTX apply -f infra/kubernetes/agent-staging/namespaces.yaml
```

File này chỉ tạo `ewallet-agents-staging`, `ewallet-sandbox-staging` và quota/ServiceAccount của chúng. Không đụng tới production.

### G.3 Giai đoạn 3 — Kiểm tra PKG-KS (chỉ đọc, sau khi namespace đã có)

```bash
KUBE_CONTEXT=$CTX scripts/llm_agent/sandbox_watch.sh kagent-staging preflight $W
KUBE_CONTEXT=$CTX scripts/llm_agent/sandbox_watch.sh kagent-staging diff $W
KUBE_CONTEXT=$CTX scripts/llm_agent/sandbox_watch.sh kagent-staging dry-run $W
```

Cả 3 lệnh phải PASS, và diff phải đúng như mục B. Nếu có lệnh nào fail thì dừng; không thay đổi cấu hình controller.

### G.4 Giai đoạn 4 — Nâng watch scope của controller (owner duyệt)

```bash
WATCH_APPROVAL=PKG-KS:$CTX KUBE_CONTEXT=$CTX scripts/llm_agent/sandbox_watch.sh kagent-staging upgrade $W
KUBE_CONTEXT=$CTX scripts/llm_agent/sandbox_watch.sh kagent-staging verify $W
```

`upgrade` tự chạy lại diff và server dry-run trong cùng lần chạy rồi mới `helm upgrade --atomic`, đồng thời ghi lại revision trước upgrade. `verify` kiểm tra Agent và SandboxAgent production vẫn Ready.

Nếu upgrade hoặc verify fail:

```bash
WATCH_APPROVAL=PKG-KS-ROLLBACK:$CTX KUBE_CONTEXT=$CTX scripts/llm_agent/sandbox_watch.sh kagent-staging rollback $W
```

Lệnh này đưa release về revision đã ghi lúc upgrade, sau đó dừng campaign.

### G.5 Giai đoạn 5 — Chuẩn bị còn lại (mỗi bước cần owner duyệt riêng, theo thứ tự)

1. Apply RBAC đã duyệt (`agent-staging-cd.proposed.yaml`), rồi chạy các lệnh `kubectl auth can-i` ở C.2.
2. `python -m ops.llm_agent.staging_credentials create --context $CTX --approve`. Bước này phải chạy trước bước 5, vì Redis staging đọc `feast-staging-redis-auth`.
3. Copy 2 registry ConfigMap (chỉ là ConfigMap, không phải Secret):
   ```bash
   kubectl -n ewallet-mcp get cm feast-registry-fraud-online -o jsonpath='{.binaryData.registry\.db}' | base64 -d > $W/fraud-registry.db
   ```
   Kiểm tra sha256 như G.1 bước 7, rồi chạy:
   ```bash
   kubectl -n ewallet-agents-staging create cm feast-registry-fraud-staging --from-file=registry.db=$W/fraud-registry.db
   ```
   Làm tương tự với RAG. Sau đó đọc lại sha của ConfigMap staging.
4. Tạo drift ConfigMap:
   ```bash
   python -m ops.llm_agent.agi_inputs drift-configmaps --context $CTX --namespace ewallet-agents-staging --suffix staging \
     --baselines $I/drift/baselines.json --window-manifest $I/drift/window/window-manifest.json --approve
   ```
   Ở đây `I=~/ewallet-evidence/agents/integration/agi-20261115T145608Z/inputs`. Kiểm tra sha như ở A.1.
5. Apply `feast-redis.yaml` và `fraud-predictor.yaml`, rồi chờ hai Deployment Ready.
6. Seed dữ liệu. Bước này **đọc Redis production**, nên cần owner review và duyệt riêng việc copy các row này sang staging:
   - Connection string phải dùng tên DNS của Service (không dùng `127.0.0.1`) và có `,db=N`.
   - Nên dùng credential chỉ đọc cho source nếu có. Hiện Feature Store Redis chỉ có `requirepass` (không có ACL), nên quyền chỉ đọc chỉ được đảm bảo ở mức code: tool chỉ gọi `DUMP`/`SCAN`/`GET`/`TYPE`/`PTTL`/`HGETALL`/`INFO` trên source.
   - Lấy password production từ Secret `feature-store/redis`, key `password` (Secret mà StatefulSet `feature-store/redis` dùng cho `--requirepass`; đọc giá trị Secret production chỉ khi owner duyệt), đặt `EWALLET_SEED_SOURCE` và `EWALLET_SEED_TARGET`. **Không** tự mở port-forward; truyền `--context` để script tự mở:
     ```bash
     export EWALLET_SEED_SOURCE="redis.feature-store.svc.cluster.local:6379,password=…,db=0"
     export EWALLET_SEED_TARGET="feast-staging-redis.ewallet-agents-staging.svc.cluster.local:6379,password=…,db=1"
     python -m ops.llm_agent.staging_feature_seed copy fraud --context $CTX              # plan, no write
     python -m ops.llm_agent.staging_feature_seed copy fraud --context $CTX --approve
     python -m ops.llm_agent.staging_feature_seed verify fraud --context $CTX
     ```
   - Làm lại với `rag` (`db=3` → `db=4`). Lưu file JSON digest làm evidence.

### G.6 Jenkins E2E

1. Jenkins đã chạy snapshot của SHA này (G.0): ref `release/agent-staging` nằm trong bundle. Push nhánh lên GitHub chỉ để truy vết, Jenkins không đọc nó.
2. Chạy job `edai2-cd-agent-staging` với các tham số `EXPECTED_GIT_SHA`, `RELEASE_BRANCH=release/agent-staging`, `DEPLOY_APPROVED=true`, rồi bấm Approve.
3. Preflight sẽ dừng với `MISSING_PREREQUISITE` nếu thiếu bất kỳ Secret, ConfigMap hay dependency nào trong 10 Secret, 4 ConfigMap và 2 dependency đã pin.
4. Cleanup:
   - Executor tự xóa fleet và gỡ release.
   - Sau đó owner kiểm tra `HELM_DRIVER=configmap helm list -n ewallet-agents-staging`: phải rỗng.
   - Đo lại prefix RustFS staging.
   - Thu hồi credential: `staging_credentials revoke --approve`.
   - Watch scope (rollback PKG-KS) và namespace staging chỉ gỡ khi owner muốn.

### G.7 Managed Prompt runtime (row 34; operator chạy, mỗi bước ghi cần owner duyệt)

Profile riêng, tách khỏi Level A: `values-staging.yaml` + [`values-staging-managed-prompt.yaml`](../../../infra/helm/llm-agent-platform/values-staging-managed-prompt.yaml). Chạy **sau** G.6 (Level A đã cleanup, `helm list -n ewallet-agents-staging` rỗng) và sau job `edai2-cd-prompt-management` ở release SHA (job này publish `fraud-coordinator-intake/v1` lên Prompt Registry). Không dùng lại vLLM production: agentgateway production xác thực bằng `vllm-api-key` production, và muốn tới nó phải sửa NetworkPolicy/AuthorizationPolicy production. Staging dùng agentgateway và vLLM CPU riêng (cùng image model theo digest, model nằm trong image nên không cần egress). Thêm khoảng 2,1 vCPU / 6,2 GiB (vLLM 2 CPU / 6 GiB, agentgateway 100m); `vllm-fallback` có 0 replica.

```bash
A=ewallet-agents-staging; W=<campaign dir>/managed-prompt; mkdir -p $W
# 1. Two fresh staging LLM keys (agentgateway client key, vLLM API key); refuses if either exists
python -m ops.llm_agent.staging_credentials create --profile managed-prompt --context $CTX --approve
# 2. Registry -> validated ConfigMap (GET only on the Registry; SHA checked against prompts/manifest.json)
kubectl --context $CTX -n ewallet-prompts port-forward svc/prompt-registry-agentregistry 12121:12121 &
python3 ops/llm_agent/prompt_registry.py configmap --agent fraud-coordinator-intake --version v1 \
  --name fraud-coordinator-intake-prompt-managed --namespace $A > $W/prompt-configmap.yaml
kubectl --context $CTX create -f $W/prompt-configmap.yaml
# 3. Install the profile. The eight --set-string values are the ones agent_staging.helm_install_args uses in G.6:
#    the g3-byo-coordinator and mcp-tools digests of the release SHA, and the staging drift ConfigMaps' values.
HELM_DRIVER=configmap helm --kube-context $CTX install llm-agent-staging infra/helm/llm-agent-platform -n $A \
  -f infra/helm/llm-agent-platform/values-staging.yaml -f infra/helm/llm-agent-platform/values-staging-managed-prompt.yaml \
  <the eight --set-string values> --wait --timeout 20m
# 4. One natural-language request through the staging A2A Gateway (same request as agent_replay.sh nl-fraud)
kubectl --context $CTX -n $A port-forward svc/a2a-gateway 18183:8083 &
#    POST message/send to http://127.0.0.1:18183/api/a2a/ewallet-agents-staging/fraud-coordinator/ -> $W/answer.json
# 5. Read-only captures and the verdict (MANAGED_PROMPT_RUNTIME_VERIFIED, exit 0)
kubectl --context $CTX -n $A get cm fraud-coordinator-intake-prompt-managed -o json > $W/configmap.json
kubectl --context $CTX -n $A logs deploy/fraud-coordinator --all-containers > $W/coordinator.log
python3 scripts/ci/managed_prompt_verify.py --configmap $W/configmap.json --coordinator-log $W/coordinator.log \
  --answer $W/answer.json --out $W/managed-prompt-verdict.json
# 6. Cleanup (always, also after a failure)
HELM_DRIVER=configmap helm --kube-context $CTX uninstall llm-agent-staging -n $A --wait
kubectl --context $CTX -n $A delete cm fraud-coordinator-intake-prompt-managed --ignore-not-found
python -m ops.llm_agent.staging_credentials revoke --profile managed-prompt --context $CTX --approve
HELM_DRIVER=configmap helm --kube-context $CTX list -n $A        # empty
```

Verdict cần đủ 4 kiểm tra: ConfigMap đúng prompt đã pin (version, SHA-256 tính lại trên `system.md`, source `registry`, namespace staging); Coordinator log `nl_prompt.resolved` đúng prompt và không có prompt khác; có `POST /v1/chat/completions` tới **agentgateway staging** với HTTP 200 (URL production làm FAIL); câu trả lời có lựa chọn tool của LLM với số token > 0 (`nl-intake/v1`). Nếu Level A fleet không còn, câu trả lời có thể là `partial`, verdict vẫn đúng vì chỉ đánh giá chuỗi prompt → LLM.

## H. Hotfix sau review của owner

### H.1 Kiểm tra endpoint của seed Redis

- **Lỗi cũ:** `check_endpoints` so sánh hostname lấy từ cờ `--source-host`/`--target-host` do operator tự khai. Một connection string trỏ nhầm (ví dụ tới Redis production) vẫn qua được nếu cờ ghi đúng tên Service staging.
- **Thay đổi:**
  - Bỏ hai cờ trên. Identity giờ được kiểm tra trên connection string đã parse (host, port 6379, DB) trước khi mở bất kỳ kết nối nào.
  - Chế độ in-cluster: kết nối thẳng tới DNS của Service đã được kiểm tra.
  - Chế độ `--context`: script tự mở `kubectl port-forward` tới đúng `feature-store/svc/redis` và `ewallet-agents-staging/svc/feast-staging-redis`, dùng lại `scripts/ci/sandbox_candidate.kubectl_forward`. Port local được đọc từ output của chính process con, nên ràng buộc context → namespace → Service → port local → client Redis được thiết lập ngay khi tạo. Port-forward do operator tự mở (`127.0.0.1`) bị từ chối.
  - Thêm một lớp kiểm tra cuối: source và target phải có `run_id` khác nhau, nên cùng một server sau hai tên vẫn bị từ chối trước khi ghi.
- **Vì sao an toàn hơn:** không còn giá trị nào do operator khai được coi là bằng chứng về identity. Target production bị chặn ở 3 lớp: host/DB, Service của port-forward, và run_id. Các bảo vệ cũ giữ nguyên: key giới hạn theo fixture, RAG ≤ 64 key, owner marker, target phải rỗng, `RESTORE` không `REPLACE`, so digest, `--approve`, không log credential.
- **Test:** `test_staging_prerequisites.py`:
  - endpoint đúng được chấp nhận;
  - các trường hợp bị từ chối: sai Service target, production làm target, sai Service source, sai mapping DB, `127.0.0.1`, sai port;
  - cờ hostname cũ không còn tồn tại;
  - không kết nối và không ghi khi kiểm tra fail;
  - chế độ `--context` chỉ kết nối qua port-forward của chính nó tới 2 Service cố định;
  - cùng server bị từ chối;
  - `_forward` dùng lại `kubectl_forward`.

### H.2 Thứ tự PKG-KS trong runbook

- **Lỗi cũ:** runbook chạy `sandbox_watch.sh kagent-staging preflight/diff/dry-run` trước khi tạo namespace staging, trong khi preflight bắt buộc 2 namespace đã tồn tại. Trên cluster sạch, runbook sẽ fail ngay bước đó.
- **Thay đổi:** chỉ sửa tài liệu (G.1–G.5): kiểm tra chỉ đọc → tạo namespace → preflight/diff/dry-run → upgrade có approval và verify (rollback nếu fail) → các bước chuẩn bị còn lại. Script, semantics của approval, exact diff và rollback giữ nguyên.
- **Vì sao an toàn hơn:** thao tác ghi đầu tiên chỉ là tạo namespace riêng của staging. Controller dùng chung chỉ bị thay đổi sau khi preflight, diff và dry-run đều PASS trên cluster thật.
- **Test:** `test_staging_prerequisites.py::RunbookOrderTests` kiểm tra thứ tự lệnh trong mục G với chính điều kiện preflight của `sandbox_watch.sh`.
