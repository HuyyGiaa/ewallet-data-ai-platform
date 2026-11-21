# G3-B final readiness: DNS fail-closed, Dynu offline, unattended cleanup, PDB feasibility

Offline only. No change to GKE (0 nodes), Dynu, LoadBalancers or certificates; no watchdog
installed. Outside calls: read-only DNS queries, read-only kubectl reads of the sleeping
cluster, and Dynu's public documentation. Base e9f0c628. Procedure:
[`EXTERNAL_GATEWAY_RUNBOOK.md`](../../../../runbooks/edai2/EXTERNAL_GATEWAY_RUNBOOK.md), section *Unattended (overnight) campaign*.

## A. dns_check.py fail-open → fail-closed

- **Problem.** `dig +short` printed nothing both for "no record" and for a timeout, SERVFAIL, REFUSED or a dead server, so `--expect-offline` could pass on an unanswered question.
- **Change.** Each query parses dig's header. It counts only with exit 0, a status of NOERROR or NXDOMAIN, and the `aa` flag at the authoritative servers. Records are parsed as IP addresses. The NS lookup follows the same rules.

  | Verdict | Exit | Meaning |
  |---|---|---|
  | `DNS_*_OK` | 0 | state proven |
  | `DNS_*_NOT_YET` | 1 | valid answers, wrong state |
  | `DNS_CHECK_FAILED` | 2 | anything unprovable |

  `--released-ip` asserts the old LB address is gone; non-expected values stay withheld.
- **Validation** (`tests/ops/test_dns_campaign.py`):
  - valid online; offline by NXDOMAIN and by NOERROR/NODATA; approved offline IP;
  - stale LB address, home address, unexpected AAAA, partial propagation and mixed nameservers give NOT_YET, with values withheld;
  - timeout, SERVFAIL, REFUSED, unreachable, malformed, empty output with exit 0, and a malformed record give `DNS_CHECK_FAILED` in both modes;
  - a non-authoritative answer from an authoritative server fails, as does an NS lookup timeout, SERVFAIL or empty result;
  - CLI exit codes 0/1/2.

  Real read-only run today: `DNS_OFFLINE_NOT_YET` (`dns-status-now.jsonl`); an unreachable resolver gives `DNS_CHECK_FAILED`.

## B. Dynu safe-offline

Documented by Dynu (sources in the runbook research):
- The IP Update Protocol is `https://api.dynu.com/nic/update`, with Basic auth using a **separate IP update password** or its MD5/SHA-256.
- Parameters include `myip=<IP>` and `myip=no`/`myipv6=no`, which "block" a family.
- Signoff `offline=yes` shows the panel's "offline message" or "redirect to URL".
- Response codes include `good`, `nochg`, `badauth`, `nohost`, `911` (suspend 10 min) and `abuse`.

**Not documented, therefore UNVERIFIED:** what either offline mode does to the DNS answer (no A record, or a Dynu address?), and how API v2 `ipv4=false` behaves.

`scripts/edge/dynu_dns.py` implements `online --ip` and `offline --mode no-ipv4|signoff`:
- credentials come from a mode-600 file outside the repository and go only to `api.dynu.com`, in the Authorization header;
- only the configured hostname is touched, and nothing is sent without `--execute`;
- every update is followed by a bounded `dns_check.py` loop, and only `*_OK` counts.

Tests use a loopback server: parameters for each action, credentials only in the header, error answers fail, an unproven DNS state fails, a dry run sends nothing, and other endpoints and unsafe or partial credential files are refused.

**Verdict: `DNS_AUTOMATION_NEEDS_OWNER_CONFIGURATION`.** It needs the Dynu IP update credentials and one owner-approved offline verification (runbook). That verification would also take the names off the home address they point to today.

## C. Unattended cleanup

`scripts/edge/g3b_campaign.py` (init, run, cleanup, watchdog, status), with systemd user units in `scripts/edge/systemd/` and `install_watchdog.sh`:
- **State:** atomic `state.json` holding the fields the brief requires, and no secrets.
- **Deadline:** 3 h absolute from wake.
- **Intent before action:** wake, LB and DNS intents are persisted before each command.
- **Cleanup:** idempotent and resumable with bounded retries. DNS is proven offline before the LB is released; otherwise the LB is kept, the nodes still sleep, and the result is `CLEANUP_INCOMPLETE` with `ALERT.txt`. Guarded sleep runs only with the PDB approvals; an unapproved blocker gives `SLEEP_BLOCKED` and no bypass. After sleep come `verify --expect sleep` and an actual node count.
- **Watchdog:** acts when the deadline has passed, or when the heartbeat is stale and the pid is gone. A lock prevents double cleanup, and it gives up after 3 attempts.

| Layer | Guarantee |
|---|---|
| cleanup in `run` | phase failure, deadline, SIGTERM/SIGINT |
| watchdog (systemd user timer) | dead main process (`kill -9`, crash, Claude or terminal gone) while the laptop is on and online |
| cloud fallback | **not built**; design, IAM, cost and risk in the runbook; needs owner approval |

Tests (simulated commands, real processes): full PASS; a failed phase stops new work and cleans up; deadline expiry; real SIGTERM; real `kill -9` plus the watchdog CLI; `kill -9` **inside** wake, open-lb and dns-online, all cleaned up; unsafe DNS keeps the LB, reports INCOMPLETE and later resumes to COMPLETE; sleep blocked without the PDB approval, with no sleep and no resize command; invalid state; watchdog idle, ok, gave-up, and no interference with a live main process; missing preconditions fail their gate; a real run refuses with failed gates (no command runs); the units render and pass `systemd-analyze verify`.

A full CLI simulation recorded 33 commands (`simulated-command-order.jsonl`): every phase, then dns-offline, lb-release, record-manifest, sleep (with exactly the scoped approval variables) and verify-sleep.

## D. PDB feasibility (read-only, `pdb-feasibility.jsonl`)

| Item | Result |
|---|---|
| Predicted blockers after `wake-full` from the G3-A1 manifest (83 workloads, checksums OK) | `ewallet-agents/fraud-coordinator`, `ewallet-mcp/drift-mcp`, `ewallet-mcp/feature-rag-mcp`; unchanged from the G3-B1 closure |
| Not covered by the approved record | `ewallet-mcp/feature-rag-mcp` |
| KEDA | ScaledObjects currently paused by `g3a1-…`; `wake-full` unpauses them, so the sleep needs the KEDA pause approval again |

The approval proposal is in the runbook (what, why, why not inherited, how to verify).

| Pre-wake gate | Status today |
|---|---|
| `CLOUD_LAB_RECOVERY_MANIFEST_VALID` | PASS |
| `PDB_APPROVAL_COMPLETE` | FAIL until the owner approves `feature-rag-mcp` for the campaign |
| `KEDA_SLEEP_APPROVAL_READY` / `GUARDED_SLEEP_PLAN_VALID` | pending owner approval |
| `UNATTENDED_CLEANUP_READY` | FAIL: Dynu credentials, offline verification, watchdog install, linger, and laptop risk accepted or cloud fallback |
| `CLIENT_ADDRESS_APPROVED` | pending: needs the CIDR file |

## Tests

| Check | Result |
|---|---|
| compile, `bash -n` (15 scripts), systemd verify | OK |
| G3-A1 edge render | byte-identical |
| charts, cloud_lab | unchanged since e9f0c628 |
| `tests/ops` | 403/403 (DNS, Dynu and campaign: 61) |
| component CI | 450, unittest OK |
| kind | not rerun: no chart or `local_validate.sh` change |
