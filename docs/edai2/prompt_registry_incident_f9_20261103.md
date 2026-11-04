# Incident F9: unintended write to the GKE prompt registry (2026-11-03)

Status: **closed**. The registry was restored from Git, there was no production impact, and the probe tooling is fixed.

Severity: low impact, process failure. An executor error mutated a live system during a review that had been declared read-only.

## 1. What happened

| Time (UTC) | Event | Evidence |
|---|---|---|
| 11:04:56 | The executor runs an ad hoc probe heredoc against the GKE registry through a port-forward. The heredoc was intended to send only `POST /v0/apply?dryRun=true`. | Session transcript |
| 11:05:02 | Port-forward opened to `ewallet-prompts/prompt-registry-agentregistry-8687d9b776-9fq7r` by the operator account. It is the only port-forward in the 10:55–11:15 window. | `registry-security/incident-audit-gke.txt` (GKE Cloud Audit Logs) |
| ≈11:05:06 | The probe sends `POST /v0/apply?dryRun=false` with header `Authorization: Bearer forged`. The body is the Prompt `drift-specialist-system` tag `v2`, with content set to Git v2 + `"\nprobe\n"` and description `security probe`. The registry accepts it and **overwrites v2**. | `gke-endpoints.txt`; `incident-unintended-write.txt` |
| 11:06:45 | Detection: the executor sees `verify-all` / resolver output reporting a sha256 MISMATCH. The registry v2 bytes are exactly Git v2 + `\nprobe\n` (sha `d0174b5e…`). The resolver refuses v2. | `incident-unintended-write.txt` |
| 11:06:47 | Recovery: `registry_catalog publish --all-prompt-versions` from Git. The 17/17 readback matches. v2 `updatedAt` = 11:06:47.273Z. | `restore-publish.json`; registry GET |
| 11:06:50 | `verify-all` → **12/12 MATCH**. | `gke-verify-all-after-restore.json` |
| 14:13 | Re-check: `verify-all` 12/12 MATCH. The M3 gate (`post`) passes. | `m1-apply/08-verify-all.txt`; `m3-gate-gke-post.txt` |

**Exposure window:** at most about 1 min 45 s (≈11:05:06 to 11:06:47).

## 2. Root cause

The heredoc contained this line:

```python
s,_=req("POST", B+"/v0/apply?dryRun=false", ..., {"Authorization":"Bearer forged"})
print("NOT executed: ...") if False else None
```

The intent was to *describe* a write without sending it. The guard (`if False`) was applied to the `print`, while the request on the line before ran unconditionally. Nothing structural separated read-only probing from writing:

- The same helper sent GET and POST.
- `dryRun` was a literal in a URL string.
- Write tools defaulted to `http://127.0.0.1:12121`. That address belongs to whichever registry is port-forwarded at the time: kind, local Docker or GKE.

## 3. Impact

- **Effective prompts: none.**
  - Production `llm-agent` r32 reads the Git ConfigMap `drift-specialist-prompt-v2` (sha `0c0798e1…`), not the registry.
  - No managed ConfigMap exists in production, since r33 is not approved.
  - The only resolver call in the window refused the tampered bytes.
- **Registry consumers:** no automated consumer reads the registry.
- **Other principals:** no one else could have observed or used the tampered object.
  - Jenkins had port-forward rights then, but runs no prompt job (0 hits in the pipeline sources).
  - The audit log shows no other port-forward.
- **Other objects:** v1 and the other prompts were not written (`updatedAt` unchanged). Only v2 was overwritten in place (same `uid`). Description and labels were restored together with the content.

## 4. Remaining uncertainty

- agentregistry 0.4.0 does not log HTTP requests, and the registry keeps no version history. The exact send time (≈11:05:06) is therefore inferred from the port-forward audit entry plus the script's `sleep 4`; no server record exists.
- The audit log proves who opened port-forwards, but it cannot show the traffic inside the tunnel. "No other write" rests on two points: no other principal tunnelled in, and in-cluster ingress is limited to the `ewallet-prompts` namespace, whose only pods are the registry and its Postgres (F5).
- Postgres was not inspected at row level. The REST readback is the integrity check, and it matched Git byte for byte.

## 5. Corrective actions (all done 2026-11-03)

| # | Action | Where | Verified |
|---|---|---|---|
| C1 | Committed, read-only-by-default probe `scripts/llm_agent/registry_security_probe.py`. All mutating requests go through `Probe.write`. A live write needs `--allow-live-write`, plus `--write-target NAME/TAG` equal to the document, a name prefixed `security-probe-`, and no Git-pinned prompt. A dry-run probe that changes the registry exits 2 (INCIDENT). | `tests/llm_agent/test_registry_security_probe.py` (11 tests) | Throwaway local registry: `registry-security/probe-guard-local/validation.txt` steps 5–8 |
| C2 | `registry_catalog publish` has no default URL. A live publish needs `--confirm-live-write` equal to `--registry`. | same test file | step 3 |
| C3 | M3 gate: `prompt_registry gate --phase pre\|post`. `configmap` runs the post gate first. A live `registry_catalog publish` runs pre and post. Restore-from-Git (`--restore-from-git`) may cross tampered bytes only in the pre phase. | `tests/llm_agent/test_prompt_registry.py` | steps 1, 2, 4, 9, 10; GKE post gate PASS (GET only) |
| C4 | M1: Jenkins `pods/portforward` in `ewallet-prompts` removed, closing the only non-human path to the write API. | `docs/evidence/edai2/cicd_runtime_20261103/m1-apply/` | Forbidden on GKE |
| C5 | Rule: no further mutating or destructive security experiments on the live registry. Write-path behaviour is exercised only on throwaway local registries, as in C1–C3. | this document | — |
