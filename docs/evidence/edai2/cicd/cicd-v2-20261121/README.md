# CI/CD V2 release evidence (2026-11-20 to 2026-11-21)

This is the published evidence for [CICD_RELEASE_EVIDENCE.md](../../../../edai2/cicd/CICD_RELEASE_EVIDENCE.md).

**How it was copied.** Every file is a byte-preserving copy (`cp -p`) of the archive Jenkins kept on the `ewallet-dev` controller, or of an owner screenshot. Nothing was edited, and machine-generated verdicts are unchanged. `SHA256SUMS` covers every file in this directory.

**Raw archive.** The complete raw archive stays outside Git, in the owner's `~/ewallet-evidence/cicd/final/`. It holds full console logs, `build.xml` files and the offline sink log.

## Layout

| Directory | Jenkins build | Source SHA |
|---|---|---|
| `jenkins/` | controller overview (early in the campaign) | n/a |
| `prompt-management-5/` | `edai2-cd-prompt-management` #5 | `e6b190ed97b0ce8e2ff79be293063922bdfe355d` |
| `mcp-agents-3/` | `edai2-cd-mcp-agents` #3 | `e6b190ed97b0ce8e2ff79be293063922bdfe355d` |
| `coordinator-2/` | `edai2-cd-coordinator` #2 | `e6b190ed97b0ce8e2ff79be293063922bdfe355d` |
| `agent-staging-2/` | `edai2-cd-agent-staging` #2 | `7f841ecdeb66d45fe4a7207fadd84518941be8ba` |
| `rag-pipeline-11/` | `edai2-cd-rag-pipeline` #11 | `7f841ecdeb66d45fe4a7207fadd84518941be8ba` |
| `streaming-3/` | `edai2-cd-streaming` #3 | `e73e85a3ecac81461d73ab790f367bb82b1f0976` |
| `streaming-failures/` | streaming #1 `ci-verdict.json`; streaming #2 Cloud Build record | `7f841ecd…`, `da8822c9…` |
| `gke-sleep/` | guarded sleep of campaign `cicd-closure-20261121T093155Z` | n/a |

Each build directory contains:
- `json/`: `ci-verdict.json`, `release-request.json`, `release-plan.json`, `image-manifest.json`, `image-scan.json`, `source-metadata.txt`, `release-verdict.json` and the unit verdicts `unit-*.json`;
- `screenshots/`, where available.

## Notes on specific files

**Renamed files.** These keep their bytes but were given new names:

| Published file | Original file |
|---|---|
| `streaming-3/screenshots/STREAMING_Stage_View_After_Release_Gate.png` | `Screenshot From 2026-11-21 15-55-05.png` |
| `streaming-3/screenshots/STREAMING_Build_Artifacts.png` | `Screenshot From 2026-11-21 16-15-44.png` |
| `streaming-3/json/*.log.txt` | the archived `*.log` files (the repository ignores `*.log`) |
| `gke-sleep/*.txt` | copies in which the local kubeconfig path was replaced with `<redacted-path>`; the originals are in the raw archive |

**Agent staging screenshots.** `agent-staging-2/screenshots/CICD_14`–`CICD_17` illustrate the Agent staging runtime: workloads, the kagent Coordinator, the SandboxAgents and the WorkerPool. They are component illustrations. The pass/fail evidence for the successful run is `unit-agent-staging-verdict.json`, together with `CICD_18` and `CICD_19`.

**Not published.**
- Screenshots `CICD_13` and `CICD_24` are excluded by owner decision.
- No `CICD_02` or `CICD_05` was ever captured.
- No screenshot of the Streaming #3 approval dialog exists. The approval is recorded in the Jenkins console (`Approved by admin`) and in `release-request.json`.

## Checks

Before publication the following checks were run:
1. **Integrity.** Every screenshot was matched by sha256 to an original in the owner's screenshot folder, and every JSON file parses.
2. **Text secret scan.** The text files were scanned for GCP access tokens, authorization headers, credential-bearing URLs, ServiceAccount JWTs, private keys, kubeconfig data and passwords. No match was found.
3. **Visual review.** Each screenshot was opened and reviewed by eye. The text scan does not cover image content.
