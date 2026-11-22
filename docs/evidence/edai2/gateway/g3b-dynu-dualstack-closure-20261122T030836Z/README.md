# G3-B Dynu dual-stack DNS closure (offline code patch + read-only DNS)

No change to Dynu (the owner's manual *Offline* state is kept), GKE (0 nodes), LoadBalancers or
certificates. Base d26a25cb. Files changed: `scripts/edge/dns_check.py`, `scripts/edge/dynu_dns.py`,
`scripts/edge/g3b_campaign.py`, `tests/ops/test_dns_dualstack.py` (new), and the runbook
(*Dynu offline: dual-stack*, *DNS cleanup*).

## Owner observations (2026-11-22)

- `dynu_dns.py offline --mode no-ipv4` (`myip=no`) → Dynu `nochg`; the old IPv4 stayed → `DNS_OFFLINE_NOT_YET`.
- Panel *Go Offline Now* → A `162.216.242.207`, AAAA `2602:ff23:0:8888::207` on every name.
- `dns_check.py --expect-offline --offline-ip <A>` → still NOT_YET: every AAAA counted as unexpected.

## Changes

| Area | Change |
|---|---|
| `dns_check.py` | `--offline-ipv6`. Offline: A ∈ {none, approved IPv4} and AAAA ∈ {none, approved IPv6}, never the released LB; "none" only as a proven NXDOMAIN/NODATA. Online is unchanged: A = LB and no AAAA at all. Address arguments are type-checked (IPv4 vs IPv6) and canonicalised with `ipaddress`. Queries run concurrently (about 3–4 s instead of minutes). The last line summarises verdict, failed servers and names, A/AAAA status, check errors and duration. Exit codes 0 / 1 / 2 unchanged |
| `dynu_dns.py` | `offline --offline-ipv6`, type-checked and forwarded to `dns_check`. One progress line per verification attempt (no addresses). Ctrl+C during verification sends nothing more (`INTERRUPTED`, exit 130). Dynu's word is reported as `update_status`; only the DNS verdict decides. Still dry run by default; credentials only in the Authorization header |
| `g3b_campaign.py` | `--dns-offline-ipv6`, stored in the private config and validated (`SAFE_CLEANUP_READY` fails on a wrong family). Default `--dns-offline-mode signoff`. Cleanup forwards both offline addresses and `--released-ip`, with bounded verification (`--attempts 10 --interval 30`). Phases, L4/L5, ACME, Cloud Lab, certificates and Grafana are untouched |

## Read-only DNS now (`dns-offline-*.jsonl`, `offline-target-identity.txt`)

| Check | Verdict |
|---|---|
| dual-stack (`--offline-ip 162.216.242.207 --offline-ipv6 2602:ff23:0:8888::207`) | `DNS_OFFLINE_OK` in 4.2 s: 6 Dynu NS + 1.1.1.1 + 8.8.8.8 × (bare name, 4 service names, random wildcard) × A/AAAA, 0 errors |
| IPv4 only | `DNS_OFFLINE_NOT_YET` with `AAAA_status: NOT_OK` (the owner's finding, now explicit) |

Identity of the offline targets: both reverse-resolve to `offline.dynu.com`, and they sit in
the same 162.216.242.0/24 and 2602:ff23:0:8888::/64 ranges as `api.dynu.com`. `offline.dynu.com`
itself has no forward A/AAAA record, so this is reverse DNS plus range evidence, not
forward-confirmed reverse DNS. The addresses are inputs, not code defaults, and are re-read
before each campaign.

## Not verified

`DYNU_SIGNOFF_LIVE_VERIFICATION: NOT_RUN`. Whether the API `offline=yes` produces the panel's
dual-stack offline state needs the owner-approved test in the runbook. Until then,
`--dns-offline-verified` must not be set.

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_dns_dualstack.py` | 40 passed |
| regression (dual-stack, dns_campaign, g3b_supervised, public_preflight, edge_scripts, negative_checks, session_a_charts) | 367 passed |
| full `tests/ops` | 500 passed |
