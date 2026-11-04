# Jenkins Platform CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xây dựng root Jenkins pipeline modular, component-aware, fail-closed với uv governance, controlled CD policy và execution-bound evidence cho E-Wallet monorepo.

**Architecture:** Root `Jenkinsfile` và compatibility `Jenkinsfile.llm-agent` chỉ là entrypoint; cả hai dùng cùng repo-local Groovy helper và Python CLI. `jenkins/catalog/components.json` là nguồn sự thật duy nhất cho ownership, dependency closure, commands, artifacts và deployability; evidence/matrix được sinh từ catalog và execution thật.

**Tech Stack:** Jenkins Pipeline/Groovy, Python 3 standard library, Bash strict mode, Git, uv, Docker, Helm, Trivy.

**Spec:** `docs/superpowers/specs/2026-11-01-jenkins-platform-cicd-design.md`

## Global Constraints

- Chỉ làm việc trong `/home/huyy-giaa/Develop/ewallet-pc-cicd` trên `codex/pc-cicd-20261101`; không tạo worktree/branch, merge, push, rebase hoặc chạm main/canonical/Observability.
- Catalog là nguồn sự thật duy nhất cho ownership, dependency mapping và pipeline scope.
- Chín uv projects giữ nguyên `pyproject.toml`/`uv.lock`; CI chỉ dùng `uv lock --check` và `uv sync --locked`.
- Deployment tắt mặc định; không cloud/GKE operation, push image hoặc production trigger trong implementation campaign.
- Component `extension-only` không thể xuất hiện trong deployment plan.
- Static/local/Jenkins/CD verification là các trạng thái tách biệt; không suy diễn runtime PASS.
- Chỉ sửa CI/CD-owned files và dependency contract test; không sửa business/runtime contracts.

## Review Focus

- Target ref không tồn tại hoặc không có merge-base phải chọn full active validation, không trả scope rỗng (Task 2).
- Shared path hoặc upstream change phải lan truyền đến tất cả dependent components mà không lan sang unrelated component (Task 2).
- Missing tool, command failure và security failure phải có evidence/failure classification ngay cả khi stage thất bại (Task 3).
- Catalog malformed, dependency cycle, unknown project hoặc extension-only deploy target phải fail closed (Task 1 và Task 5).
- Release request với wrong branch, mutable tag, mismatched SHA hoặc thiếu approval phải bị từ chối trước bất kỳ external command nào (Task 5).

---

### Task 1: Authoritative component and Python-project catalog

**Files:**
- Create: `jenkins/catalog/components.json`
- Create: `scripts/ci/catalog.py`
- Create: `scripts/ci/__init__.py`
- Create: `tests/ci/test_catalog.py`
- Create: `tests/ci/__init__.py`
- Modify: `tests/unit/test_uv_dependency_contract.py`

**Interfaces:**
- Consumes: repository paths, existing tests/scripts/Dockerfiles and nine existing `pyproject.toml` + `uv.lock` pairs.
- Produces: `load_catalog(path) -> Catalog`, `Catalog.validate(root) -> None`, `Catalog.python_projects`, `Catalog.active_components`, `Catalog.matrix_markdown()`.

- [ ] **Step 1: Write failing behavior tests** for nine exact Python projects, real source paths/commands, unknown dependency rejection, cycle rejection, missing lock rejection and extension-only deploy rejection.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.ci.test_catalog tests.unit.test_uv_dependency_contract` and verify failures name the missing catalog/omitted inference and serving projects.
- [ ] **Step 3: Implement minimal catalog loader/validator and declarative catalog** without changing any dependency file; update the existing uv contract inventory to all nine projects.
- [ ] **Step 4: Re-run the targeted tests** and expect all pass except tool-dependent skips explicitly reported.
- [ ] **Step 5: Review status/full diff, then commit** `feat(ci): add authoritative component catalog`.

### Task 2: Merge-base change detection and dependency closure

**Files:**
- Create: `scripts/ci/change_detection.py`
- Create: `tests/ci/test_change_detection.py`
- Modify: `ops/llm_agent/changed_components.py`

**Interfaces:**
- Consumes: Task 1 `Catalog`, changed paths, current SHA and optional validated target ref.
- Produces: `detect_scope(catalog, paths, full_reason=None) -> Scope`, `resolve_git_scope(...) -> Scope`, JSON containing merge base, files, components and `full_validation_reason`.

- [ ] **Step 1: Write failing tests** for multi-commit merge-base diff, direct path selection, shared path fan-out, reverse dependency closure, unrelated isolation, unknown path fallback and missing target/no merge-base full validation.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.ci.test_change_detection` and verify failure because the new API does not exist.
- [ ] **Step 3: Implement deterministic detection** using `git merge-base` and `<base>...HEAD`; adapt the legacy module to delegate path mapping to the catalog API.
- [ ] **Step 4: Re-run detection and legacy LLM tests**; expect selected scope literals from fixtures and no regression.
- [ ] **Step 5: Review status/full diff, then commit** `feat(ci): add merge-base component detection`.

### Task 3: Locked execution, failure classification and evidence

**Files:**
- Create: `scripts/ci/run_ci.py`
- Create: `scripts/ci/evidence.py`
- Create: `tests/ci/test_run_ci.py`
- Create: `tests/ci/test_evidence.py`

**Interfaces:**
- Consumes: Task 1 `Catalog`, Task 2 scope JSON, workspace/tool lookup and build metadata environment.
- Produces: strict sequential selected-component execution; `classify_failure(stage, exit_code, tool_state) -> str`; execution manifest, per-command logs, lock hashes, component matrix and `SHA256SUMS` even on failure.

- [ ] **Step 1: Write failing tests** proving exact uv command order (`lock --check`, `sync --locked`, tests), isolated project cwd, missing-tool classification, nonzero exit propagation, no shell interpolation, failure evidence and checksum reproducibility.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.ci.test_run_ci tests.ci.test_evidence` and verify expected missing APIs.
- [ ] **Step 3: Implement minimal subprocess-list runner and atomic evidence writer**; never regenerate locks and never treat a skipped/missing command as PASS.
- [ ] **Step 4: Re-run targeted tests**, then run a bounded real static execution to `docs/evidence/ci_cd/<run-id>/`; preserve its real success/failure state.
- [ ] **Step 5: Review status/full diff, then commit** `feat(ci): add locked execution evidence`.

### Task 4: Root Jenkins orchestration and LLM compatibility

**Files:**
- Create: `Jenkinsfile`
- Create: `jenkins/pipeline/componentPipeline.groovy`
- Create: `tests/ci/test_jenkins_contract.py`
- Modify: `Jenkinsfile.llm-agent`
- Modify: `tests/llm_agent/test_ci_scripts.py`

**Interfaces:**
- Consumes: catalog validator, scope/evidence CLIs and existing LLM scripts.
- Produces: root/profile entrypoints with shared checkout/provenance, detect, CI, report/archive and disabled-by-default release gates.

- [ ] **Step 1: Write failing behavioral contract tests** that execute/parse the shared entrypoint configuration and prove both Jenkinsfiles delegate to one helper, root defaults deploy off, exact SHA is required, archived evidence is non-empty and no `HEAD^ HEAD` remains.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.ci.test_jenkins_contract tests.llm_agent.test_ci_scripts` and verify failures reflect missing root/shared helper.
- [ ] **Step 3: Implement the shared Groovy pipeline helper and thin entrypoints**; use `set -euo pipefail` for shell steps and preserve existing LLM script compatibility through the catalog/profile.
- [ ] **Step 4: Re-run Jenkins/LLM contract tests** and available static syntax checks; report Jenkins/Groovy runtime unavailable instead of claiming runtime PASS.
- [ ] **Step 5: Review status/full diff, then commit** `feat(ci): add modular Jenkins orchestration`.

### Task 5: Controlled CD release policy

**Files:**
- Create: `scripts/ci/release_policy.py`
- Create: `scripts/ci/deploy_release.sh`
- Create: `tests/ci/test_release_policy.py`
- Create: `tests/ci/test_deploy_release.py`

**Interfaces:**
- Consumes: Task 1 catalog, exact checkout SHA, release branch allowlist, explicit approval/environment/context and immutable image manifest.
- Produces: validated dependency-ordered deployment plan containing only active deployable units; shell dispatcher reuses approved existing deployment scripts and refuses external action before policy PASS.

- [ ] **Step 1: Write failing tests** for default denial, branch/SHA mismatch, mutable image rejection, target allowlist, extension-only exclusion, dependency ordering and zero external calls on rejected requests.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.ci.test_release_policy tests.ci.test_deploy_release` and verify expected missing APIs/scripts.
- [ ] **Step 3: Implement fail-closed policy/dispatcher** with no embedded credentials and no `latest`; local tests use fake executables only.
- [ ] **Step 4: Re-run policy/dispatcher tests** and existing LLM deploy/rollback fake-tool tests; do not invoke real registry/cloud/cluster commands.
- [ ] **Step 5: Review status/full diff, then commit** `feat(ci): gate immutable releases`.

### Task 6: Indexed documentation, final evidence and verification

**Files:**
- Create: `docs/cloud/jenkins_cicd.md`
- Modify: `docs/README.md`
- Modify: `README.md`
- Modify: generated real-run files under `docs/evidence/ci_cd/<run-id>/`
- Modify: `docs/llm_agent/living_decision_drafts.md` only if adding the copy-ready entry does not misrepresent canonical synchronization; otherwise include the entry only in final handoff.

**Interfaces:**
- Consumes: all implementation and actual local execution results.
- Produces: indexed architecture/runbook/release/rollback/reproduction/rubric mapping/limitations; evidence checksums; copy-ready Living Decision entry.

- [ ] **Step 1: Document from actual catalog/evidence** without copying an independent ownership matrix; link the matrix artifact generated by `catalog.py`.
- [ ] **Step 2: Run full practical validation**: CI unit/contracts, existing LLM CI tests, uv contract, compileall, catalog validation, local runner, `git diff --check`, secret/literal scans and Docker-only checks when safely available.
- [ ] **Step 3: Record unavailable tools and unexecuted runtime gates** as UNVERIFIED/MISSING_RUNTIME_DEPENDENCY, never PASS.
- [ ] **Step 4: Inspect complete status/diff and evidence checksums**, correct documentation mismatches only with matching tests where behavior changes.
- [ ] **Step 5: Commit focused documentation/evidence** with `docs(ci): document Jenkins workflow evidence`.
- [ ] **Step 6: Run final whole-branch verification and review**, then prepare Vietnamese handoff with commit SHAs, files, test counts, evidence paths and Living Decision copy-ready entry.
