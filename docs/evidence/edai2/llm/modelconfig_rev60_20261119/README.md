# BYO ModelConfig Resolver: `llm-agent` revision 60 evidence

This folder publishes the evidence of how the BYO ModelConfig Resolver was configured in Helm release `llm-agent` revision 60. Revision 60 was installed on GKE `ewallet-dev` on 2026-11-19 at 09:00:45Z (16:00:45 ICT) and was still the deployed revision when the cluster was put to sleep. It is referenced from [LLM_PLATFORM.md §5.5](../../../../edai2/llm/LLM_PLATFORM.md#55-status-and-rollout).

All files were derived from artifacts that the Agent Platform campaigns wrote at the time, kept in the owner's local evidence store. No cluster was contacted to produce this folder, and the originals were not modified.

## What this evidence covers

| Evidence kind | Files | What they show |
|---|---|---|
| Helm history and upgrade | `01`, `02` | Revision 60 is `deployed`; revision 59 is `superseded` |
| Change from revision 59 to 60 | `03` | The upgrade removed 9 retired `feature-rag-mcp` objects and changed one ConfigMap. It did not add, remove or change the ModelConfig, the Role, the RoleBinding, the NetworkPolicy or the Coordinator `Agent` |
| Rendered Kubernetes configuration | `04`, `05` | The manifest installed as revision 60, of which `05` is an extract (link explained below) |
| Effective Helm configuration | `06` | `helm get values llm-agent` at revision 60: `modelConfig.byoResolver.enabled: true` |
| Live deployment read-back | `07` | The Coordinator Deployment created by kagent under revision 60, and its pods, read twice on 2026-11-19 |

**Not covered: runtime execution.** No `modelconfig.resolved` log line, no Coordinator pod log and no `kubectl auth can-i` result was saved while revision 60 was deployed. The searches for this are listed below. Pod readiness in `07` is a read-back of Kubernetes status. It does not show that the resolver ran or succeeded.

## How the rendered manifest is tied to revision 60

1. The upgrade step that created revision 60 (campaign `lf-agents-20261119T081448Z`, stage 1) recorded the SHA-256 of the manifest it rendered and installed: `04-rev60-render-sha256.txt` = `baee38bd…f7d1`.
2. The same campaign's offline gate had rendered that manifest on 2026-11-18 at 16:42Z, into `lf-preflight/gate-equivalent/private/llm-agent-on.yaml`. Its SHA-256 is also `baee38bd…f7d1` (see the source table).
3. `05` therefore extracts objects from the exact manifest that the upgrade installed. The diff (`03`) shows that none of these objects changed between revisions 59 and 60.

## Files

| File | Type | Source (relative to the owner evidence store) | Collected | Revision |
|---|---|---|---|---|
| `01-helm-history-after-upgrade.txt` | Original, byte-identical copy | `agents/replay/replay-20261118T161231Z/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-history-after.txt` | 2026-11-19 09:03:03Z, after the upgrade (the file shows ICT) | 59 → 60 |
| `02-helm-upgrade-output.txt` | Original, byte-identical copy | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-upgrade.txt` | 2026-11-19 09:03:01Z (release time 09:00:45Z) | 60 |
| `03-rev59-to-rev60-diff.txt` | Original, byte-identical copy | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-diff.txt` | 2026-11-19 09:00:44Z, one second before the upgrade (live 59 vs rendered 60) | 59 → 60 |
| `04-rev60-render-sha256.txt` | Original, byte-identical copy | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-render-sha256.txt` | 2026-11-19 09:00:44Z, upgrade step | 60 |
| `05-rendered-rev60-modelconfig-objects.yaml` | Extract, IPv4 redacted | `agents/replay/replay-20261118T161231Z/lf-preflight/gate-equivalent/private/llm-agent-on.yaml` | rendered 2026-11-18 16:42Z | 60 (by render hash) |
| `06-live-values-rev60-modelconfig.yaml` | Extract, IPv4 redacted | `agents/replay/replay-20261118T161231Z/g4-registry-readback-20261119/registry/llm-agent-live-values.yaml` | 2026-11-19 10:42:49Z (G4 registry read-back) | 60 |
| `07-live-coordinator-rev60.json` | Extract | `agents/replay/replay-20261118T161231Z/g4-platform-status-20261119/status/{helm-llm-agent,deployments-ewallet-agents,pods-ewallet-agents}.json` and the same three files under `g5-preflight-20261120/status/` | 2026-11-19 10:33Z (G4) and 17:15Z (G5 preflight) | 60 (each snapshot includes the Helm status read in the same run) |

**Extract contents.**

- `05`: six objects of the rendered manifest, unchanged except for IPv4 redaction: `ModelConfig ewallet-global-llm`, `Role` and `RoleBinding ewallet-global-llm-reader`, `NetworkPolicy allow-byo-modelconfig-to-kube-apiserver`, `ServiceAccount fraud-coordinator` and the kagent BYO `Agent fraud-coordinator`. The `Agent` shows the resolver environment, the `kube-api-modelconfig` projected token volume and the Secret references (names and keys only; the manifest contains no Secret values).
- `06`: only the `modelConfig` block of the Helm values.
- `07`: for each snapshot, the Helm status of `llm-agent`; for the `fraud-coordinator` Deployment, its owner, generation, deployment revision, ServiceAccount, the LLM-selection environment variables (`EWALLET_LLM_*`, `EWALLET_NL_INTAKE_ENABLED`, `EWALLET_KUBE_API_TOKEN_DIR`), whether `EWALLET_LLM_BASE_URL` and `EWALLET_LLM_MODEL` are absent, the `kube-api-modelconfig` volume and mount, and the replica status; for each Coordinator pod, start time, Ready time and restart count. A `valueFrom` variable is published as its Secret name and key only.

**Redaction.** The API-server ClusterIP and endpoint addresses in `05` and `06` are replaced with `<REDACTED-IPV4>`. Nothing else was changed in the extracted fields. The originals were not copied, because they also contain unrelated values and other workloads' configuration.

## What the live read-backs show, and their limits

- **Both snapshots** report Helm revision 60 `deployed`. The Coordinator Deployment is owned by `Agent/fraud-coordinator`, has deployment revision 300, sets `EWALLET_LLM_CONFIG_SOURCE=modelconfig` and `EWALLET_LLM_MODELCONFIG_NAME=ewallet-global-llm`, has no `EWALLET_LLM_BASE_URL` or `EWALLET_LLM_MODEL`, and mounts the `kube-api-modelconfig` projected volume (ServiceAccount token, 3600 s; cluster CA; namespace).
- **G4 snapshot (10:33Z).** Both pods started at 07:05:54Z, **before** the upgrade to revision 60 (09:00:45Z), and ran through it with 0 restarts. Revision 60 did not change the Coordinator (`03`), so the pod template they run is the one revision 60 describes, but any start-up resolution in these pods happened under revision 59.
- **G5 preflight snapshot (17:15Z).** After the interim sleep and wake, both pods were new: they started at 17:10:13Z and 17:10:14Z, under revision 60, and were Ready at 17:13:13Z and 17:13:27Z, each after 2 container restarts (the cold-start restarts noted in the Agent operations report).
- **Why readiness is not proof of resolution.** The resolver is designed to stop the Coordinator at start-up if resolution fails, so a Ready pod is consistent with a successful resolution. The read-back records only Kubernetes status, not the resolver's output, so it is not treated as runtime evidence.

## Searches for runtime evidence (none found for revision 60)

- `modelconfig.resolved` across the whole evidence store: found only in `agents/integration/agi-20261115T145608Z/stage2/AGI-01-*` (2026-11-15) and `agents/phase2/p2-foundation-20261116T084657Z/load/coordinator-logs.raw` (2026-11-16). Both are earlier than revision 60 and are not used here.
- The names of the four revision-60 Coordinator pods (`…-69wfm`, `…-hcndr`, `…-8d88n`, `…-zzctv`): found only in the status read-backs `pods-ewallet-agents.json`, never in a log file.
- A live `helm get manifest` or RBAC `can-i` read-back taken under revision 60: none. The only live manifest read-back in this replay is for revision 59 (`lf-preflight/sha-rca/private/live-manifest-rev59.yaml`), which is not used here.

## Source SHA-256 (originals, unchanged)

| SHA-256 | Source |
|---|---|
| `fb1b1634aa6270d3acf2e7b3fe8db76a5f0bfd410dbf73c915bfa4fc24d12f14` | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-history-after.txt` |
| `387f115abb8ce03150798268aa5e391b770d6cdfdf0558aa1bbdf24faf3994f1` | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-upgrade.txt` |
| `7ba468137582c0d5fbe3ba899654fb4122b714c5e220ae3279641e7d21bdb551` | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-diff.txt` |
| `39049261956cada60dae59b24c5e45897185c69b2ef07583746b62e9448d532d` | `…/lf-agents-20261119T081448Z/agi/stage1/llm-agent-lf-agents-render-sha256.txt` |
| `baee38bd0b0d2eeff5f5c9de087a97cbaf120aa547d3846d6e7865151944f7d1` | `…/lf-preflight/gate-equivalent/private/llm-agent-on.yaml` |
| `f614e76f17ed94188080412ce466949ca1c0bd4f1ee334bd066bfd5f4107b497` | `…/g4-registry-readback-20261119/registry/llm-agent-live-values.yaml` |
| `2a5ecbb367273dbe7fea6fc2357d09757bad754c6b9521cce48c04c9eb983e0a` | `…/g4-platform-status-20261119/status/helm-llm-agent.json` (the G5 preflight copy has the same hash) |
| `8f2c6ec830e05149535801083a2cf4b0d7f59ce4ac3f93387341e430e6079f0a` | `…/g4-platform-status-20261119/status/deployments-ewallet-agents.json` |
| `d85fe22fda6f04855357b5bd76f878fa6af3a7cb84f6200d5b9541fd019551f8` | `…/g4-platform-status-20261119/status/pods-ewallet-agents.json` |
| `a599e8cf667193e6654c7eeca87ea18daf890a3b756be278a87789955b4f5c93` | `…/g5-preflight-20261120/status/deployments-ewallet-agents.json` |
| `4d0b59d8ac84221224e7f7f75e45616b9fa1766cc6ba5e0ea4a29c794af615b1` | `…/g5-preflight-20261120/status/pods-ewallet-agents.json` |

`…` stands for `agents/replay/replay-20261118T161231Z`. The SHA-256 of every published file is in [`SHA256SUMS.txt`](SHA256SUMS.txt); check them with `sha256sum -c SHA256SUMS.txt` from this folder. The byte-identical copies `01`–`04` have the same hash as their sources.
