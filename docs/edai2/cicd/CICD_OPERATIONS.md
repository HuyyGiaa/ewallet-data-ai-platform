# CI/CD V2 Operations

This guide explains how to operate CI/CD V2 on the `ewallet-dev` GKE cluster:
- preparing a release;
- running a release job and inspecting its results;
- the failures seen during acceptance and how they were fixed;
- recovery;
- the guarded sleep and wake of the cluster.

The design behind each step is in [CICD_ARCHITECTURE.md](CICD_ARCHITECTURE.md). The results of the real runs are in [CICD_RELEASE_EVIDENCE.md](CICD_RELEASE_EVIDENCE.md).

Every command below is either **read-only** or **state-changing**. State-changing commands are marked ⚠ and need the owner's approval. Commands assume the repository root and:

```bash
export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig
CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
AR=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
```

## Contents

1. [Operating model](#1-operating-model)
2. [Preparing a release](#2-preparing-a-release)
3. [Running a release job](#3-running-a-release-job)
4. [Inspecting a release](#4-inspecting-a-release)
5. [Streaming campaign prerequisites](#5-streaming-campaign-prerequisites)
6. [Troubleshooting the verified failures](#6-troubleshooting-the-verified-failures)
7. [Failure handling and recovery](#7-failure-handling-and-recovery)
8. [GKE lifecycle: guarded sleep and wake](#8-gke-lifecycle-guarded-sleep-and-wake)

## 1. Operating model

A release involves two roles:
- **The operator** prepares the source snapshot, rolls out the controller and verifies results.
- **The owner** triggers jobs and approves gates in the Jenkins UI. Approving the `input` step is a human decision that the pipeline records; no script answers it.

The Jenkins UI is reachable only through a port-forward:

```bash
kubectl --context $CTX -n ewallet-ci port-forward svc/jenkins 18080:8080   # then http://127.0.0.1:18080
```

The controller pod is recreated on every rollout, so the port-forward has to be reopened after each one.

## 2. Preparing a release

Jenkins reads only the Git bundle in its snapshot image ([CICD_ARCHITECTURE.md §3.2](CICD_ARCHITECTURE.md#32-why-a-git-bundle-instead-of-a-git-remote)). Every code change Jenkins must use therefore follows the sequence below. It was run for each release SHA in this campaign, the last time for `e73e85a3` at 07:56–08:01Z on 2026-11-21.

**1. Fix the release and base SHAs** (read-only). The release SHA must be pushed; the base must be an ancestor of it.

```bash
R=$(git rev-parse HEAD); BASE=98b71e30e51914f25ad17974debffadbdaca2487
git status --short | wc -l                                   # 0
test "$(git rev-parse origin/feature/edai2-cicd-v2)" = "$R"
```

**2. Build and verify the bundle** (local files only):

```bash
W=~/ewallet-evidence/cicd/snapshot-$R; mkdir -p $W
python3 scripts/ci/source_snapshot.py build  --sha $R --base $BASE --out $W/snapshot
python3 scripts/ci/source_snapshot.py verify --dir $W/snapshot --sha $R     # {"problems": [], "verdict": "PASS"}
grep -E '^(source_sha|bundle_sha256)=|release/streaming=' $W/snapshot/source-metadata.txt
```

**3. ⚠ Build and push the snapshot image, then compare digests.** Artifact Registry tags are immutable, so check first that the tag does not exist yet. `--provenance=false` keeps a single-platform manifest.

```bash
gcloud artifacts docker images list $AR/jenkins-source-snapshot --include-tags --filter="tags:$R" --format='value(version)'   # empty
docker buildx build --platform linux/amd64 --provenance=false -f infra/docker/jenkins-source-snapshot/Dockerfile \
  -t $AR/jenkins-source-snapshot:$R --metadata-file $W/metadata.json --push $W/snapshot
python3 -c "import json;print(json.load(open('$W/metadata.json'))['containerimage.digest'])"
gcloud artifacts docker images describe $AR/jenkins-source-snapshot:$R --format='value(image_summary.digest)'   # must match
```

**4. Render the controller manifest** (local). [render_jenkins_runtime.py](../../../scripts/ci/render_jenkins_runtime.py) accepts only digest references.
- Keep the current controller image unless the controller itself changed.
- Remove the PersistentVolumeClaim from the rendered set: the live PVC was resized to 32 GiB and must not be re-applied.

```bash
python3 scripts/ci/render_jenkins_runtime.py --controller-image $AR/jenkins-controller@sha256:<current> \
  --source-image $AR/jenkins-source-snapshot@<digest> --source-sha $R --base-sha $BASE \
  --ml-sha $R --agent-sha $R --docs-sha $R \
  --jenkins-gsa ewallet-jenkins@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com --output $W/rendered.yaml
python3 -c "import sys,yaml; print('---\n'.join(yaml.safe_dump(d, sort_keys=False) for d in yaml.safe_load_all(open(sys.argv[1])) if d and d['kind'] != 'PersistentVolumeClaim'))" $W/rendered.yaml > $W/apply.yaml
```

**5. Diff and server dry-run** (read-only). The only expected change is the `jenkins` Deployment: the `ewallet.io/source-sha` annotation, the snapshot image and the four SHA variables. That was 14 changed lines in this campaign. Any other change is drift and stops the rollout.

```bash
kubectl --context $CTX diff -f $W/apply.yaml
kubectl --context $CTX apply --dry-run=server -f $W/apply.yaml     # 1 configured, 7 unchanged
```

**6. ⚠ Apply, but only when Jenkins is idle** (no busy executor, empty queue). Then verify the result.

```bash
kubectl --context $CTX apply -f $W/apply.yaml
kubectl --context $CTX -n ewallet-ci rollout status deployment/jenkins --timeout=15m
kubectl --context $CTX -n ewallet-ci exec deploy/jenkins -c jenkins -- sh -c \
  'grep -E "^(source_sha|bundle_sha256)=" /opt/ewallet-source/source-metadata.txt; git bundle list-heads /opt/ewallet-source/repository.bundle | grep release/'
kubectl --context $CTX -n ewallet-ci logs deploy/jenkins -c jenkins | grep -c SEVERE    # 0
```

After the rollout, confirm three things:
- the job list is unchanged (11 jobs);
- the build history is intact;
- the PVC `jenkins-home` is still `Bound 32Gi`.

The `Recreate` strategy means a short Jenkins outage on every rollout.

## 3. Running a release job

Open the job, choose **Build with Parameters**, and enter values that match the snapshot. These are the values of Streaming Build #3:

| Parameter | Value | Why |
|---|---|---|
| `EXPECTED_GIT_SHA` | `e73e85a3ecac81461d73ab790f367bb82b1f0976` | Checkout must equal it exactly. |
| `TARGET_BRANCH` | `98b71e30e51914f25ad17974debffadbdaca2487` | Immutable base SHA in the bundle, used for change detection. |
| `RELEASE_BRANCH` | `release/streaming` | Must resolve to `EXPECTED_GIT_SHA` inside the bundle. |
| `DEPLOY_APPROVED` | `true` | Without it the build is CI-only. |
| `ENVIRONMENT` / `KUBE_CONTEXT` | `gke-cpu` / `in-cluster` | `KUBE_CONTEXT` must be explicit; the default kubeconfig context is never used. |
| `IMAGE_MANIFEST` | empty | Build or reuse through Cloud Build. A reviewed manifest skips that stage. |
| `REGISTRY_LOOKUP` | `required` | Reuse and digest readback; `off` always builds and records `SKIPPED_BY_PARAMETER`. |
| `PROMOTE_TO_PRODUCTION` | `false` | The production handoff path was not used ([CICD_ARCHITECTURE.md §10](CICD_ARCHITECTURE.md#10-the-production-boundary)). |
| `INTENTIONAL_GATE_FAILURE` | `false` | `true` only for the negative proof job. |
| `STREAMING_SERVICE_ACCOUNT` | `ewallet-cicd-streaming` | Streaming only. Empty means the streaming unit is `BLOCKED`. |
| `FEAST_SNAPSHOT_ID`, `FEATURE_RUN_ID` | `feast-r5gb-20261111-214544`, `ab-20261110225456-optimized-feat-r261110225459` | Streaming only, owner-supplied campaign inputs. |

The Agent jobs use the same parameters with their own `release/<group>` and SHA, and leave the streaming fields empty.

Component CI runs first. With the `platform` profile (streaming) it took about 36 minutes in this campaign. The build then pauses at **Controlled release gate**. The approver should check that the SHA and release branch in the prompt are the expected ones before clicking **Approve release**. The console records `Approved by <user>`, and that id is written into `release-request.json`.

## 4. Inspecting a release

### 4.1 Jenkins artifacts

Every build archives `evidence/ci/jenkins-<job>-<n>/` (Build → **Build Artifacts**). The files to read, in order:

| File | Content |
|---|---|
| `jenkins-build.txt`, `source-metadata.txt` | Job, build, triggering user, SHA; the snapshot's refs and bundle sha256 |
| `scope.json`, `ci-verdict.json`, `junit/` | Changed files, selected components; CI status and test counts |
| `release-request.json`, `release-plan.json` | Approved request; verified plan with units, digests and manifest sha256 |
| `image-manifest.json`, `build/` | Per image: digest, revision, Cloud Build id, source object, reuse, readback |
| `image-scan.json` | Per image: `status`, `findings`, `excepted`, `expired_exceptions` |
| `release/release-verdict.json` | `verdict`, `stage`, `production_changed`, locks, per-unit exit codes |
| `release/<unit>/…` | The unit's own verdict (`verdict.json`, `staging-verdict.json`, streaming campaign files) |

The archive can also be read without the UI (read-only):

```bash
kubectl --context $CTX -n ewallet-ci exec deploy/jenkins -c jenkins -- \
  cat /var/jenkins_home/jobs/edai2-cd-streaming/builds/3/archive/evidence/ci/jenkins-edai2-cd-streaming-3/release/release-verdict.json
```

### 4.2 Cloud Build and Artifact Registry

Read-only checks. Never print access tokens or URLs that contain credentials.

```bash
gcloud builds list --project ewallet-data-ai-platform-hg26 --region asia-southeast1 --limit 10   # builds are regional
gcloud builds describe <build-id> --project ewallet-data-ai-platform-hg26 --region asia-southeast1 --format='value(status,results.images)'
gcloud artifacts docker images describe $AR/flink-block-a:<sha> --format='value(image_summary.digest)'
```

The digest from Artifact Registry must equal the `digest_ref` in `image-manifest.json`.

### 4.3 Rehearsal workloads

**Bounded rehearsals** park their Deployments in `ewallet-cicd` at zero replicas. After a successful release you should see:
- `coordinator-cd`, `fleet-cd-*` and `mcp-cd-*` at `0/0`;
- the CronJob `rag-ingestion` suspended;
- no smoke Job or ephemeral Redis left behind.

```bash
kubectl --context $CTX -n ewallet-cicd get deploy,cronjob,jobs
```

**Agent staging** removes everything it created. Only the operator-applied dependencies `feast-staging-redis` and `fraud-detector-predictor` remain in `ewallet-agents-staging`.

### 4.4 Streaming campaign

Workloads appear in `ml-platform`, labelled `app.kubernetes.io/part-of=ewallet-block-a`. They exist only while the campaign runs, about ten minutes in Build #3:
- Deployments `block-a-redpanda`, `block-a-flink-jobmanager` and `block-a-flink-taskmanager`;
- Jobs `block-a-topics`, `block-a-publish`, `block-a-feast`, `block-a-offline`, `block-a-history` and `block-a-validate`;
- the Feast apply Job `fs-apply-cicd-<sha12>-<build>`.

```bash
kubectl --context $CTX -n ml-platform get deploy,jobs,pods -l app.kubernetes.io/part-of=ewallet-block-a
kubectl --context $CTX -n ml-platform port-forward deploy/block-a-flink-jobmanager 18081:8081   # Flink UI
```

The Flink job is submitted only after `block-a-topics` completes. A dashboard opened earlier shows *Running Jobs: No Data* until it is reloaded. Completed Job pods show `0/1` ready; that is normal.

The port-forward goes through the kubelet, so NetworkPolicy B2 does not block it. Do not loosen B2 to reach the UI.

The verdict files are archived under `release/streaming-block-a/verify-<namespace>/`:
- `consistency-report.json`, the full report;
- `consistency-verdict.json`, the operator's `check-report` result;
- `flink-jobs.json`, the Flink REST state at collection time;
- `validator-job.json`;
- one log per sink.

The fs-apply Job carries `ttlSecondsAfterFinished=86400` and does not have the Block A labels. Kubernetes removes it after 24 hours; it is not left behind by the cleanup.

## 5. Streaming campaign prerequisites

The streaming unit needs B1–B4 in place ([CICD_ARCHITECTURE.md §11.2](CICD_ARCHITECTURE.md#112-streaming-prerequisites-b1b4)). All four checks below are read-only:

```bash
S=system:serviceaccount:ewallet-ci:ewallet-cicd-streaming
kubectl --context $CTX auth can-i create serviceaccounts/ewallet-cicd-streaming --subresource=token -n ewallet-ci \
  --as=system:serviceaccount:ewallet-ci:jenkins                                      # B1: yes
kubectl --context $CTX auth can-i create pods/redis-0 --subresource=exec -n feature-store --as=$S   # B1: yes
kubectl --context $CTX auth can-i list secrets -n ml-platform --as=$S                               # B1: no
kubectl --context $CTX diff -f infra/kubernetes/streaming-block-a/jobmanager-ingress.networkpolicy.yaml   # B2: no diff
gcloud storage buckets get-iam-policy gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data --format=json  # B3: two cicd-streaming-* conditions
kubectl --context $CTX -n feature-store exec redis-0 -c redis -- redis-cli -n 10 dbsize                  # B4: target DB state
```

Before a new streaming run, consider three things:
- **The target Redis DB must be empty or owned by the same campaign.** DB 10 is now owned by `verify-cicd-e73e85a3ecac-3`, so a new campaign needs another database or an owner decision to free DB 10. Do not flush a database to get a run through.
- **The B3 bindings expire on 2026-11-27.** After that date the sink Jobs cannot write to GCS.
- **Never read or print the Redis Secret.** The B4 change was made with a script that edits only the `db=` option in memory and guards the patch with the Secret's resourceVersion.

## 6. Troubleshooting the verified failures

### 6.1 Streaming Build #1: `ss` missing on the controller

**Symptom.** Component CI failed, with 1 failure out of 2329 test cases. The failing test was `tests/experiments/test_vllm_bench_session.py::test_port_in_use_stops_the_session`.

**Cause.** The streaming job uses the `platform` profile, which includes the `ci-platform` component. That component's test command runs `tests/experiments` ([components.json](../../../jenkins/catalog/components.json)). `scripts/experiments/vllm_bench.sh` checked local ports with `ss`, which the controller image does not ship. The guard therefore could not see a listener, and the test that expects a refusal failed.

**Fix (`da8822c9`).** `port_listener` now probes with a Python socket and has three exit codes ([vllm_bench.sh L212-L251](../../../scripts/experiments/vllm_bench.sh#L212-L251)):

| Exit code | Meaning | Result |
|---|---|---|
| 0 | Port free | Start |
| 1 | A listener answered | Refuse to start |
| 2 | The check itself failed | Refuse to start (fail closed) |

IPv6 errors on `::1` (`EAFNOSUPPORT`, `EADDRNOTAVAIL`, `ENETUNREACH`) are tolerated. Tests cover every port in use without `ss`, free ports without `ss`, and a probe that cannot run. The test file passes locally: 55 passed, 2 skipped.

**Lesson.** A CI component can test tools that only an operator workstation normally runs. Any binary such a test relies on must exist in the controller image, or the test must avoid it.

### 6.2 Streaming Build #2: relative registry redirect

**Symptom.** Component CI passed, and Cloud Build `5c998f9d-5154-4652-a653-f498d0b50230` built `flink-block-a` successfully. The stage then failed with:

```text
ValueError: unknown url type: '/artifacts-downloads/namespaces/…'
```

**Cause.** After a build, `cloud_build.py` reads the tag back and fetches the image config blob to check the provenance labels. Artifact Registry answered the blob request with `302` and a **relative** `Location` header. The client passed that value straight to `urllib.request.Request`, which only accepts absolute URLs.

**Fix (`e73e85a3`).** The client resolves the location with `urljoin` against the registry URL and refuses a non-https result ([cloud_build.py L252-L261](../../../scripts/ci/cloud_build.py#L252-L261)). As before, the redirect request carries no credential. A read-only check against the real registry showed that the redirected URL serves the blob without authentication.

The regression test `test_relative_blob_redirect_resolves_against_the_registry_host` covers three things:
- the relative redirect is resolved;
- a non-https target is refused;
- no `Authorization` header is sent.

That test fails without the fix. All 17 tests in `tests/ci/test_cloud_build.py` pass.

On Build #3, the readback succeeded for both images (`registry_readback: VERIFIED`).

**Lessons.**
- The image Build #2 pushed (`flink-block-a:da8822c9…`) was not reused by Build #3. Reuse is looked up by tag, and the tag is the release SHA, so a new SHA always rebuilds.
- During the investigation, a diagnostic `curl -w '%{redirect_url}'` with `-u` printed a short-lived token. When debugging registry redirects, print only the status code and the host.

### 6.3 Other fixes made during acceptance

| Commit | Failure | Fix |
|---|---|---|
| `b0ab052a`, `34fdfbb7` | `uv lock --check` for `rag-ingestion` needed Python 3.11, which the controller downloaded into a read-only path; the first fix then introduced Trivy findings through pip | Preinstall Python 3.10/3.11/3.12 at image build time with `UV_PYTHON_DOWNLOADS=manual`; remove the unused pip from the 3.11 interpreter |
| `28ec7f69` | The Agent staging seed check compared Redis `DUMP` bytes, which differ between servers for the same hash | Compare a canonical, type-aware digest (`canonical-v1`) ([staging_feature_seed.py](../../../ops/llm_agent/staging_feature_seed.py)) |
| `7f841ecd` | Agent staging E2E: two direct Specialist requests shared one sandbox session | Each request sets its own `contextId` ([specialist_e2e.py](../../../scripts/ci/specialist_e2e.py#L104-L118)) |

## 7. Failure handling and recovery

**Rehearsal failure.**
- Bounded and RAG rehearsals restore the previous workload, or remove a first deployment.
- Agent staging deletes exactly what it created and uninstalls its Helm release. Anything not proven gone is listed in `left_behind` and must be removed by hand after inspection.
- The dispatcher stops at the first failing unit and lists the rest in `not_run`.

**Streaming failure.** Cleanup always runs once the campaign has started. If a run was interrupted, the operator's own cleanup removes only objects carrying both campaign labels:

```bash
scripts/streaming/block_a_cloud.sh cleanup verify-cicd-<sha12>-<build>             # dry run: lists what it would delete
scripts/streaming/block_a_cloud.sh cleanup verify-cicd-<sha12>-<build> --execute   # ⚠
```

Two things stay by design, and are never deleted to "clean up" a run: the campaign's GCS prefixes and the claimed Redis database.

**Locks.** A Lease left by a killed build is taken over only after it expires, by deleting that exact resourceVersion. No manual action is needed in the normal case.

**Retrying.** A retry with the same SHA reuses images whose labels match. A retry after a code fix needs a new release SHA, a new snapshot and a controller rollout (§2).

**Rollback.** The rehearsal executors implement their own restore, as above. None of the six successful builds needed it, and this report has no evidence of a restore being exercised. The production transaction implements reverse rollback, a durable journal and `python -m scripts.ci.production_release recovery-plan --journal <file>`. None of these production paths has been exercised on the cluster. Treat them as untested procedures.

**Controller rollback.** Re-apply the previously applied manifest, which each snapshot directory keeps as `apply.yaml`, then repeat the post-rollout checks of §2. Every rollout in this campaign moved forward, so this procedure was not exercised.

## 8. GKE lifecycle: guarded sleep and wake

The cluster is operated as an on-demand lab by [cloud_lab.py](../../../ops/gcp/cloud_lab.py):
- **Read-only:** `status`, `plan-sleep`, `plan-wake`, `verify` and `cost-status`. `record-manifest` writes local files only.
- **Mutating:** every other command prints its plan by default. It acts only with `--execute --confirm-cluster ewallet-dev` and `CLOUD_LAB_APPROVED=yes`.

### 8.1 Guarded sleep

The plain resize to zero is blocked by PodDisruptionBudgets: the Coordinator and both MCP servers run two replicas each. The guarded variant needs two extra approvals:
- **Scaling the guarded Deployments to zero** needs `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<campaign>`.
- **Pausing their KEDA ScaledObjects** needs `CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:<campaign>` ([L488-L533](../../../ops/gcp/cloud_lab.py#L488-L533)). Without the pause, KEDA would scale them back up.

Both values name one campaign. They are set in the environment of that single command and never stored in files or shell profiles.

The procedure used on 2026-11-21 (campaign `cicd-closure-20261121T093155Z`):

```bash
C=cicd-closure-<UTC timestamp>; D=~/ewallet-evidence/cicd/$C; mkdir -p $D/preflight
python3 ops/gcp/cloud_lab.py verify --expect awake --manifest <previous recovery manifest>      # read-only
python3 ops/gcp/cloud_lab.py record-manifest --out $D/recovery-manifest --campaign $C --source-commit $(git rev-parse HEAD)
python3 ops/gcp/cloud_lab.py plan-sleep
python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --pause-keda --also-approve ewallet-mcp/feature-rag-mcp \
  --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z --campaign $C                 # dry run
# ⚠ only after the dry run matches the last approved plan:
CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$C CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:$C \
  python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --pause-keda --also-approve ewallet-mcp/feature-rag-mcp \
  --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z --campaign $C --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest $D/recovery-manifest
```

`record-manifest` refuses to write into an existing directory, so pass a path that does not exist yet.

The executed plan made these changes:
1. Paused KEDA on `ewallet-sandbox/business-agents-workerpool` at its floor of 2, and on `ewallet-agents/fraud-coordinator` at 0.
2. Scaled `fraud-coordinator`, `drift-mcp` and `feature-rag-mcp` to 0. All three ran 2 replicas before.
3. Resized node pool `cpu-pool` (`e2-standard-4`) to zero.

`verify --expect sleep` passes only when all of the following hold ([`sleep_failures`, L788](../../../ops/gcp/cloud_lab.py#L788-L793)):
- no node-pool VM remains;
- every `pvc-*` disk recorded in the manifest still exists;
- every recorded bucket still exists.

It passed with an empty failure list, and the pool's managed instance group reported size 0. Every PVC, including Jenkins' 32 GiB home and the Redis volumes, survives.

**Scale to zero does not make the project free.** The following still cost money:
- the GKE cluster itself, whose control plane keeps running;
- 16 persistent disks, about 90 GB;
- Artifact Registry storage;
- the GCS buckets;
- Cloud Build and Logging retention.

### 8.2 Data that does not survive

`feature-store/redis` keeps its data: append-only persistence is on and the data sits on its PVC. The Valkey cluster and the idempotency Redis also have PVCs.

**`ewallet-agents-staging/feast-staging-redis` uses `emptyDir` only, so its seeded staging features are lost at sleep.** Before the next Agent staging release, the owner reseeds it with the owner-run seed procedure and checks it with the canonical digest ([CICD_V2_P3B0_PREREQUISITES.md](../../workplans/edai2/CICD_V2_P3B0_PREREQUISITES.md)). Without the reseed, the staging preflight or E2E fails. It never falls back to production.

### 8.3 Wake

⚠ This needs owner approval and was not run as part of this documentation work:

```bash
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full \
  --manifest ~/ewallet-evidence/cicd/cicd-closure-20261121T093155Z/recovery-manifest --nodes 5 \
  --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect awake --manifest ~/ewallet-evidence/cicd/cicd-closure-20261121T093155Z/recovery-manifest
```

`wake-full` performs these steps ([`wake`, L727](../../../ops/gcp/cloud_lab.py#L727-L753)):
1. resizes the pool;
2. labels nodes for the LLM pods;
3. restores the recorded replicas, except those owned by the WorkerPool and KEDA;
4. unpauses the ScaledObjects the sleep paused.

`verify --expect awake` then also fails on any ScaledObject still paused by the cloud lab.

After the wake, complete these steps before running CD again:
1. Reseed the staging Redis (§8.2).
2. Reopen the Jenkins port-forward.
3. Re-check B1–B4 (§5).
