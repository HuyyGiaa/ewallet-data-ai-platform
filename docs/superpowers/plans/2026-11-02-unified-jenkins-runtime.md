# Unified Jenkins Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove one bounded Jenkins control plane executes immutable-source ML, Agent, change-detection, and negative-gate builds without privileged containers or public exposure.

**Architecture:** Extend the existing component catalog and shared pipeline. Run one Jenkins controller in an isolated GKE namespace behind ClusterIP and NetworkPolicy, with an immutable private Git bundle whose checked-out HEAD must equal the approved SHA. CI runs in the controller image with locked `uv` environments; deployment remains disabled for this proof.

**Tech Stack:** Jenkins LTS/JCasC, Groovy Pipeline, Python 3.12, uv, Git bundle, Kubernetes, Helm-free namespaced manifests.

**Spec:** `/home/huyy-giaa/.codex/attachments/1546406e-ac83-44e0-99b3-77836a18dc60/pasted-text.txt`

## Global Constraints

- No Docker socket, privileged container, cluster-admin credential, public Ingress, Git push, or canonical/main mutation.
- Source checkout must fail unless its full SHA equals `EXPECTED_GIT_SHA`.
- Runtime proof must include one ML PASS, one Agent PASS, and one intentional gate FAIL that never reaches deployment.
- Use existing `pyproject.toml` and `uv.lock` with `uv --locked`; do not regenerate unrelated locks.
- Keep Jenkins app-scoped and resource-bounded; preserve all G3/MLOps releases.

## Review Focus

- Stale or mismatched source bundle must fail before tests.
- Documentation-only changes must select no runtime units.
- Unknown runtime paths must trigger fail-closed full validation.
- Negative contract result must not emit a deployment-success marker.
- Controller restart must retain job definitions and build evidence for the bounded campaign.

---

### Task 1: Runtime contract and tests

**Files:**
- Modify: `jenkins/catalog/components.json`
- Modify: `jenkins/pipeline/componentPipeline.groovy`
- Create: `tests/ci/test_jenkins_runtime_contract.py`

**Interfaces:**
- Consumes: existing catalog, `scripts.ci.change_detection`, and `scripts.ci.run_ci`.
- Produces: runtime-safe profiles and immutable-checkout/negative-gate parameters used by Jenkins jobs.

- [ ] Write tests for exact SHA enforcement, ML/Agent profiles, no privileged build mechanism, and negative-gate short circuit.
- [ ] Run the focused tests and observe RED.
- [ ] Implement the minimal catalog/pipeline changes.
- [ ] Run `python3 -m unittest discover -s tests/ci -t .` and observe PASS.
- [ ] Commit the verified source.

### Task 2: Bounded Jenkins image and Kubernetes resources

**Files:**
- Create: `infra/docker/jenkins-controller/Dockerfile`
- Create: `infra/docker/jenkins-controller/plugins.txt`
- Create: `infra/kubernetes/jenkins/jenkins.yaml`
- Create: `infra/kubernetes/jenkins/README.md`
- Modify: `tests/ci/test_jenkins_runtime_contract.py`

**Interfaces:**
- Consumes: immutable Git bundle mounted at `/opt/ewallet-source/repository.bundle`.
- Produces: ClusterIP-only controller, resource limits, restricted security context, default-deny policy, and persistent bounded build state.

- [ ] Add failing manifest/image security contract tests.
- [ ] Run the focused tests and observe RED.
- [ ] Add digest-pinned Jenkins/uv sources, JCasC/job bootstrap, and namespaced resources.
- [ ] Run tests, YAML parsing, and `git diff --check`.
- [ ] Commit the verified source.

### Task 3: Immutable snapshot and actual Jenkins builds

**Files:**
- Runtime evidence only under `docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/jenkins/`.

**Interfaces:**
- Consumes: exact local HEAD, controller image, and namespace manifest.
- Produces: Jenkins build IDs, console logs, archived evidence, and screenshots.

- [ ] Create and checksum a full Git bundle that contains exact HEAD.
- [ ] Build/scan/push one immutable controller image with that bundle.
- [ ] Preflight semantic scope and deploy only namespace-local Jenkins resources.
- [ ] Execute ML PASS, Agent PASS, documentation-only selection, isolated-change selection, and intentional negative FAIL builds sequentially.
- [ ] Verify build archives, source SHA, no deployment verdict on failure, controller health, and memory headroom.

### Task 4: Cross-platform regression and closure

**Files:**
- Create: `docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/REPORT.md`
- Create: `docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/SHA256SUMS`

**Interfaces:**
- Consumes: Jenkins build evidence and P1 Prometheus/Tempo/Loki evidence.
- Produces: final runtime verdict, rollback proof, evidence index, and Living Decision copy.

- [ ] Re-run one bounded G3 Business E2E and security/idempotency health checks.
- [ ] Capture genuine Grafana/Jenkins UI screenshots.
- [ ] Record rollback commands and verify no unrelated Helm revision changed.
- [ ] Hash raw evidence, run full focused regression gates, and verify Git diff/status.
- [ ] Restore Authorized Networks to the original `/32`, stop local forwards, and commit only reviewed report/source files.
