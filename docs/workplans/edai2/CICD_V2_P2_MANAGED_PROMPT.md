# CI/CD V2-P2 — Managed prompt cho BYO Coordinator (offline)

| Mục | Giá trị |
|---|---|
| Trạng thái | `V2_P2_OFFLINE_ACCEPTED` (chứng minh offline; runtime acceptance để V2-P3) |
| Ngày | 2026-11-18 |
| Nhánh | `feature/edai2-cicd-v2`, base `96722a9e` |
| Phạm vi | Offline. Không GKE, Jenkins, publish Registry hay Langfuse. Không đổi kiến trúc BYO. |

## A. Vấn đề

- Prompt Registry (agentregistry + Git `prompts/manifest.json` có SHA-256) đã quản lý prompt của các Agent declarative.
- Nhưng Agent **duy nhất gọi LLM** là BYO Coordinator, và nó vẫn gửi hằng số `SYSTEM_PROMPT` trong `nl_intake.py`.
- Registry vì vậy chưa chứng minh được rằng một prompt được quản lý thực sự đi vào request LLM.
- Hai BYO Specialist là deterministic, không có LLM, nên không cần (và không được thêm) system prompt.

## B. Thay đổi

| File | Vai trò |
|---|---|
| `prompts/fraud-coordinator-intake/v1.md` và `prompts/manifest.json` | Prompt hiện tại, byte-identical, được đăng ký là `fraud-coordinator-intake/v1` (cùng schema manifest) |
| `ops/llm_agent/registry_catalog.py` | Publish thêm prompt intake đã pin, để Registry có nội dung này |
| `ops/llm_agent/prompt_registry.py` | ConfigMap do `configmap` tạo có thêm key `prompt.json` (agent, version, sha256, source), vì volume mount không mang annotation |
| `ai_platform/llm_agent/nl_intake.py` | `OpenAIToolPlanner(system_prompt=…)` (mặc định là `SYSTEM_PROMPT`); `system_prompt_from_env()` đọc và kiểm tra prompt được mount một lần, khi khởi động |
| `templates/agents.yaml`, `values.yaml` | `naturalLanguageIntake.managedPrompt` (mặc định **tắt**): pin version và SHA từ manifest, mount ConfigMap `fraud-coordinator-intake-prompt-managed` |
| `tests/llm_agent/test_v2p2_managed_prompt.py` | 7 test mục tiêu |

## C. Danh tính prompt

| Thuộc tính | Giá trị |
|---|---|
| Identity | `fraud-coordinator-intake/v1` (Registry name `fraud-coordinator-intake-system`, tag `v1`) |
| SHA-256 | `cbb0c51b0edad83bd5640a3d03e542c44e5070018c2b83a5565ee82c17244380` |
| Byte identity | Nội dung file đúng bằng `SYSTEM_PROMPT.encode()`, không thêm newline: `PROMPT_BYTE_IDENTICAL = PASS` |

`fraud-coordinator/v5.md` là prompt router của cấu hình declarative, **không** dùng ở đây.

## D. Tích hợp runtime (Option B: ConfigMap do Registry resolve)

**Vì sao chọn B thay vì A.**
- Image BYO chỉ chứa `ai_platform/`: không có resolver `ops/`, không có manifest, không có PyYAML.
- Option A (Coordinator tự gọi HTTP tới Registry) sẽ cần thêm policy mạng `ewallet-agents → ewallet-prompts` trên namespace production, thêm code và dữ liệu vào image.
- Option B dùng lại đúng workflow đã có, và schema kagent 0.10.3 BYO hỗ trợ `volumes`/`volumeMounts` (chart đã dùng cho token ModelConfig).

**Luồng:**

```
Prompt Registry ──(prompt_registry.py configmap: M3 post-gate + resolve, SHA so với Git manifest)──►
ConfigMap fraud-coordinator-intake-prompt-managed {system.md, prompt.json(source=registry)}
   ──(Helm mount /var/run/ewallet/prompt; env pin VERSION + SHA256 từ manifest)──►
Coordinator khởi động: system_prompt_from_env() kiểm tra version, source=registry, sha256(file) == SHA pin
   ──► OpenAIToolPlanner(system_prompt=…) ──► request LLM: messages[0] = prompt đã duyệt
```

**Fail-closed:** ở chế độ managed, Coordinator không khởi động được nếu gặp một trong các trường hợp:
- thiếu mount;
- thiếu version hoặc SHA;
- version khác;
- SHA khác hoặc file bị sửa;
- `source` là `pinned-git-fallback` hoặc nguồn lạ.

Không có fallback về hằng số. Prompt được resolve trước khi planner tồn tại, và không có polling theo từng request. Log chỉ ghi identity và SHA (`nl_prompt.resolved`), không ghi nội dung prompt.

## E. Test

| Lệnh (env khóa) | Kết quả |
|---|---|
| `pytest tests/llm_agent/test_v2p2_managed_prompt.py` (llm-agent-tooling) | **7 passed** |
| Regression liên quan, coord312: `test_n11_nl_intake`, `test_agent_ab`, `test_global_modelconfig_resolver`, `test_g3_byo_coordinator`, `test_agi_workflow_intake`, `test_v2p1_gateway_environment` | 86 passed (bằng baseline) |
| Regression liên quan, tooling312: prompt registry/catalog/readback/security probe, chart (`agents`, `agi`, `global_modelconfig`, `n11`), `test_v2p1_staging_chart` (byte ref `0fff5625`), `test_registry_gate`, NL intake, A/B | 175 passed |
| `tests/ci` cùng catalog, runbook và replay tests | 723 passed, cả trước và sau |

Bảy test mới:

1. **Byte identity:** file == `SYSTEM_PROMPT` == SHA trong manifest.
2. **Registry → mount → `planner_from_env` → request:** `messages[0] == {"role": "system", "content": <approved>}`. Mọi variant A/B nhận cùng một prompt.
3. **Chứng minh nối dây:** một version test-only có nội dung khác `SYSTEM_PROMPT` đi đúng vào request. v1 trùng hằng số nên test 2 một mình không phân biệt được.
4. **Tampered:** Registry trả nội dung lệch (`PromptTampered`), file mount bị sửa (`ManagedPromptError`), và **không có request LLM nào** được gửi.
5. **Thiếu hoặc không khả dụng:** Registry down (`RegistryUnavailable`, không tự fallback Git); thiếu mount, thiếu version, sai version, sai SHA, source lạ, hay ConfigMap tạo từ Git fallback đều lỗi.
6. **Legacy:** không bật managed thì request vẫn mang `SYSTEM_PROMPT` gốc.
7. **Helm:** bật managed thì có env pin SHA đúng manifest, volume ConfigMap và mount; volume ModelConfig vẫn giữ; spec Agent hợp lệ theo CRD kagent 0.10.3.

Mutation check: nếu `_body()` quay lại dùng hằng số, test 3 fail.

Evidence (in ra trong test):

```
Prompt identity: fraud-coordinator-intake/v1
Prompt source: registry
Prompt SHA256: cbb0c51b0edad83bd5640a3d03e542c44e5070018c2b83a5565ee82c17244380
Planner: OpenAIToolPlanner
LLM system message: approved prompt content
Verification: PASS
```

## F. Tương thích ngược

- Managed mặc định **tắt**: không cần Registry, không đổi cách khởi động, không đổi request body.
- 8 profile production vẫn `PRODUCTION_RENDER_BYTE_IDENTICAL` so với `0fff5625`.
- Profile staging của V2-P1 không đổi (NL intake vẫn tắt, isolation test PASS).
- Không đổi tool schema, temperature, max tokens, ModelConfig, Gateway, workflow Fraud/Drift, A2A/MCP hay thuật toán A/B.

## G. Giới hạn (cần runtime, V2-P3)

1. Publish prompt lên Registry live: `registry_catalog publish`, chạy M3 gate trước và sau.
2. Tạo ConfigMap bằng `prompt_registry.py configmap --agent fraud-coordinator-intake --version v1 --namespace <ns>`, rồi apply.
3. Bật `managedPrompt.enabled` cùng NL intake trên một Coordinator đã deploy (staging cần agentgateway và LLM; đây là quyết định của owner).
4. Bằng chứng runtime: log `nl_prompt.resolved` với SHA ở trên, và một request thật qua LLM Gateway.

**Row 34 chưa được coi là accepted chỉ dựa trên test offline.** Câu hỏi mở về rubric: nếu cách diễn đạt "Data Agent" đòi mọi Agent đều có prompt managed, thì Specialists deterministic là ngoại lệ có chủ đích (không gắn LLM giả để có prompt).

## H. Bước tiếp

V2-P3 cần đưa Coordinator lên với managed prompt và ghi lại evidence ở mục G. Phụ thuộc thêm các blocker staging của V2-P1 (kagent watch scope, sandbox staging, credential).
