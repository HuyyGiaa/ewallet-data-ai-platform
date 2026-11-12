# Docker and Docker Compose Optimization

This report covers EDAI-1 rubric rows 3 (Docker and Docker Compose, 2 points) and 4 (Dockerfile optimization, 3 points). The workload is tied to the 5GB campaign `ab-20261110225456` (`rehearsal-5gb-v1`). The owner runs the official experiment and captures DK01–DK05 with [DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md](../../runbooks/edai1/DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md), Stage 1.

The October image experiment (88.44 %, DataHub lineage-plan workload) is historical. It is not reused here.

## 1. Requirement → implementation

| Row | Requirement | Implementation | Evidence | Status |
|---:|---|---|---|---|
| 3 | Docker and Docker Compose | Hardened one-shot Compose service [`infra/docker/compose.edai1.yml`](../../../infra/docker/compose.edai1.yml) running the optimized image on read-only inputs | DK05 | SOURCE_IMPLEMENTED, LOCAL_RUNTIME_VERIFIED, OWNER_SCREENSHOT_PENDING |
| 4 | Dockerfile optimization with Before/After proof | [`Dockerfile.baseline`](../../../images/edai1-campaign-verifier/Dockerfile.baseline) against [`Dockerfile.optimized`](../../../images/edai1-campaign-verifier/Dockerfile.optimized), measured by [`ops/docker_ab`](../../../ops/docker_ab/) as a 2×2 matrix | DK01–DK04 | SOURCE_IMPLEMENTED, LOCAL_RUNTIME_VERIFIED, OWNER_SCREENSHOT_PENDING |

LOCAL_RUNTIME_VERIFIED means all four images were built, run and compared on this Docker host during implementation, and the Compose service completed with exit code 0. The official numbers are the owner's run, recorded in §5.

## 2. Workload

Both images run the same application: [`ops/edai1_verifier`](../../../ops/edai1_verifier/), a deterministic verifier of the committed campaign evidence. It uses the standard library only. It checks:

- the checksum chain of the four campaign documents of each profile (`dp3_completion` → `campaign_manifest` → `dp1`/`dp2` handoffs);
- the identity of every document: benchmark, profile, dataset version, source Spark Git SHA and source manifest SHA-256;
- every stage's transformation batch `SUCCEEDED` and validation `PASS`;
- the eight validation reports: bound to their run, `PASS`, and 260 rules per profile;
- the semantic comparison: `SEMANTIC_EQUIVALENT`, with 9 / 9 / 12 / 4 tables per stage;
- the Airflow record: 6 DagRuns and 54 task instances, all successful.

The expectations live in one file, [`workload-5gb.json`](../../../images/edai1-campaign-verifier/workload-5gb.json). The evidence folder and that file are bind-mounted read-only at run time, so no data is baked into an image and every variant reads byte-identical inputs. The verifier prints one summary line holding the SHA-256 of its canonical report. The report contains no timestamp, host name or absolute path. That SHA-256 is the functional fingerprint compared across images.

## 3. Baseline and optimized Dockerfiles (DK01)

| Aspect | Baseline (`Dockerfile.baseline`) | Optimized (`Dockerfile.optimized`) |
|---|---|---|
| Stages | 1 | 2: `build` and `runtime` |
| Base image | `python:3.13.9-bookworm`, with the compiler toolchain, `git` and `curl` | `python:3.13.9-slim-bookworm` (both digest-pinned) |
| Build context | Whole repository, minus the root `.dockerignore` | Only `ops/edai1_verifier/`, via `Dockerfile.optimized.dockerignore` |
| Payload | `COPY . /app` | The compiled package only, copied from `build` |
| Build-time checks | None | `compileall`, an import of every module, and `--help` must succeed or the build fails |
| Runtime user | root | UID/GID `65532`. The application files are root-owned and read-only |

The multi-stage build alone does not shrink a pure-Python image much. What it buys is a validated, minimal payload copied into a runtime stage that holds nothing else. Most of the size difference comes from the base image. Because of that, the experiment measures the two factors separately instead of crediting the total to the multi-stage build.

## 4. Experiment design

[`python3 -m ops.docker_ab`](../../../ops/docker_ab/__main__.py) builds four images on one host. The control variants change exactly one factor:

| Variant | Packaging | Base | Role |
|---|---|---|---|
| `baseline` | single-stage, full context | full Bookworm | baseline |
| `baseline-slim` | single-stage, full context | slim Bookworm | control: base-image effect |
| `optimized-full` | multi-stage, minimal payload | full Bookworm | control: packaging effect |
| `optimized` | multi-stage, minimal payload | slim Bookworm | optimized |

**Fairness.**
- Every variant uses the same source, inputs, host and architecture.
- Base images are pulled by digest before any build, so build times exclude downloads.
- Every build uses `--no-cache --pull=false`.
- Each image runs the verifier five times with no network, a read-only root filesystem and all capabilities dropped. Every run must PASS with one fingerprint.
- The result records the Docker engine, Buildx and Compose versions, base-image digests and IDs, the tool Git SHA and the source Spark Git SHA.

**Measured per variant:**
- build success and duration;
- content size (`docker image inspect .Size`);
- unpacked disk usage as `docker image ls` reports it;
- total and added layers, and bytes added by the Dockerfile;
- run status, duration (median of five) and fingerprint;
- runtime UID/GID;
- whether the application files are writable by the runtime user;
- which of `gcc`, `make`, `git`, `curl` and `apt-get` the image ships.

**Size metric.** This host uses Docker 29 with the containerd image store. On that store `.Size` is the compressed content of the image's layers, which is what a registry pull transfers. `docker image ls` reports the larger unpacked disk usage. The report names both, and the attribution uses content bytes, which are exact.

**Attribution.**
- Base-image effect = size difference between the two bases at equal packaging.
- Packaging effect = size difference between the two packagings at equal base.
- Each effect is measured at both levels of the other factor, and their difference (the interaction) is reported.

## 5. Results

**OFFICIAL RESULTS: PENDING the owner run** (Stage 1 of the runbook). The values below are filled from `result.json` of that run.

| Variant | Content bytes | Disk usage | Layers | Added bytes | Build s | Run s (median) | UID | Verdict | Report SHA-256 |
|---|---:|---|---:|---:|---:|---:|---|---|---|
| baseline | PENDING | | | | | | | | |
| baseline-slim | PENDING | | | | | | | | |
| optimized-full | PENDING | | | | | | | | |
| optimized | PENDING | | | | | | | | |

| Attribution | Bytes | Share of baseline → optimized |
|---|---:|---:|
| Base-image effect (full context / minimal payload) | PENDING | |
| Packaging effect (full base / slim base) | PENDING | |
| Total | PENDING | |

> **DK01 — Dockerfiles before/after.** OWNER_SCREENSHOT_PENDING. Shows both files side by side, with the stage count, the base, `COPY . /app` against the `COPY --from=build` payload, and the user.
>
> **DK02 — builds.** OWNER_SCREENSHOT_PENDING. Shows the tail of each `build_<variant>.log` with the final image line.
>
> **DK03 — image inspection.** OWNER_SCREENSHOT_PENDING. Shows `summary.txt` (generated from `docker image inspect`) next to `docker image ls edai1/campaign-verifier`.
>
> **DK04 — functional equivalence.** OWNER_SCREENSHOT_PENDING. Shows one run of `baseline` and one of `optimized` on the same mounts, printing the same `report_sha256`.
>
> **DK05 — Compose.** OWNER_SCREENSHOT_PENDING. Shows `docker compose … config` (hardening visible), `run --rm campaign-verifier` with `"verdict": "PASS"`, exit code 0, and an empty `docker ps -a` for the project.

## 6. Docker Compose hardening (row 3)

| Area | Setting |
|---|---|
| Inputs | Evidence folder and workload spec bind-mounted `read_only: true` |
| User | `65532:65532` |
| Filesystem | `read_only: true`, plus a 16 MiB `/tmp` tmpfs with `nosuid,nodev,noexec` |
| Network and privileges | `network_mode: none`, `cap_drop: [ALL]`, `no-new-privileges` |
| Resources | 64 PIDs, 128 MiB of memory, 0.5 CPU |
| Lifecycle | `restart: "no"`. A one-shot `run --rm`, after which the project must have no containers left |

## 7. Explanation and trade-offs

- **Where the bytes go.** The full Bookworm image carries a C toolchain and general-purpose system packages that a pure-Python job never uses. The full-context payload adds the repository source tree. The matrix reports each separately.
- **Security.** The optimized image runs as non-root, and its code is not writable by the runtime user. It ships no compiler, `git` or `curl`. `apt-get` remains, because it is part of the slim Debian base.
- **Cost of the optimization.** Two stages are more to maintain, and a dependency the verifier might need later would have to be installed deliberately. The full image would already have it.
- **Run time.** The verifier reads about 1 MB of JSON. Container start-up dominates its run time, so no run-time improvement is expected or claimed. The measurement reports what it observes.

## 8. Limitations

- **Single host.** One host and one architecture (amd64). Content sizes depend on the base-image digests, which are pinned.
- **Single image.** The experiment covers this one representative image, not every image in the repository.
- **Historical image.** `images/datahub-publisher`, the October experiment's image, is kept unchanged because the `datahub` CI component builds it. It is not part of this result.

## 9. Reproduction and code references

```bash
python3 -m ops.docker_ab --run-id <new id> --output-dir <new directory>
```

| Concern | File |
|---|---|
| Workload | `ops/edai1_verifier/` (`spec.py`, `checks.py`, `report.py`, `__main__.py`) |
| Images | `images/edai1-campaign-verifier/` |
| Harness | `ops/docker_ab/` (`variants.py`, `docker_cli.py`, `measure.py`, `report.py`, `__main__.py`) |
| Compose | `infra/docker/compose.edai1.yml` |
| Tests | `tests/unit/test_edai1_verifier.py`, `tests/unit/test_docker_ab.py` |
