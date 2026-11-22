"""Point the Dynu hostname at the edge LB, or take it offline, then verify with dns_check.py.

Uses only Dynu's documented IP Update Protocol (https://www.dynu.com/DynamicDNS/IP-Update-Protocol):
HTTPS GET https://api.dynu.com/nic/update, HTTP Basic auth with the account user name and the
separate IP update password (or its SHA-256, which Dynu accepts in its place).

  online  --ip <LB IP>         hostname=<H>&myip=<LB IP>&myipv6=no
  offline --mode no-ipv4       hostname=<H>&myip=no&myipv6=no    ("blocks" both families)
  offline --mode signoff       hostname=<H>&offline=yes          (Dynu's offline page/redirect)

Observed by the owner on 2026-11-22: ``myip=no`` answered ``nochg`` and the old IPv4 stayed in
DNS (not offline). Dynu's Offline mode (panel "Go Offline Now") answers Dynu's offline server on
BOTH families (A and AAAA, reverse DNS offline.dynu.com). Whether the API ``offline=yes``
produces the same state is not yet proven. So Dynu's answer word (good / nochg) never decides
anything: every update is followed by dns_check.py with the approved offline IPv4 and IPv6, and
only its verdict counts.

Credentials: a JSON file OUTSIDE the repository, mode 600:
  {"hostname": "ewallet-hg.gleeze.com", "username": "<dynu user>", "password_sha256": "<hex>"}
They go only to api.dynu.com, in the Authorization header; never in a URL, an argument or a log.
Only the configured hostname is ever updated. Without --execute nothing is sent.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Callable
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = "https://api.dynu.com/nic/update"
OK_CODES = ("good", "nochg")
FATAL_HINTS = {"badauth": "wrong user name or IP update password", "nohost": "hostname not in this account",
               "notfqdn": "hostname is not a valid FQDN", "abuse": "update blocked for abuse",
               "911": "Dynu asks clients to suspend updates for 10 minutes", "!donator": "feature not available",
               "numhost": "too many hostnames", "dnserr": "Dynu DNS error (retry)", "servererror": "Dynu server error (retry)"}


def load_credentials(path: str) -> dict:
    file = Path(path).resolve()
    if file == ROOT or ROOT in file.parents:
        raise SystemExit(f"{file} is inside the repository; keep DNS credentials outside it")
    if stat.S_IMODE(file.stat().st_mode) & 0o077:
        raise SystemExit(f"{file} must not be readable by others (chmod 600)")
    data = json.loads(file.read_text(encoding="utf-8"))
    missing = sorted({"hostname", "username"} - set(data))
    if not (data.get("password_sha256") or data.get("password")):
        missing.append("password_sha256")
    if missing:
        raise SystemExit(f"DNS credentials file lacks {missing}")
    return data


def params(action: str, hostname: str, ip: str | None = None, mode: str | None = None) -> dict:
    if action == "online":
        return {"hostname": hostname, "myip": ip, "myipv6": "no"}
    if mode == "no-ipv4":
        return {"hostname": hostname, "myip": "no", "myipv6": "no"}
    return {"hostname": hostname, "offline": "yes"}


def send(credentials: dict, query: dict, endpoint: str = ENDPOINT, opener: Callable = urllib.request.urlopen) -> str:
    """One update request; returns Dynu's first response word."""
    if endpoint != ENDPOINT and not endpoint.startswith("http://127.0.0.1:"):
        raise SystemExit("credentials are sent only to https://api.dynu.com/nic/update")
    secret = credentials.get("password_sha256") or credentials["password"]
    token = base64.b64encode(f"{credentials['username']}:{secret}".encode()).decode()
    request = urllib.request.Request(f"{endpoint}?{urllib.parse.urlencode(query)}", method="GET",
                                     headers={"Authorization": f"Basic {token}", "User-Agent": "ewallet-edge-dns/1"})
    with opener(request, timeout=20) as response:
        return response.read().decode(errors="replace").strip().split()[0] if response else "unknown"


def verify(domain: str, expect: list[str], attempts: int, interval: int, run: Callable = subprocess.run,
           sleep: Callable = time.sleep, out: Callable = print) -> tuple[str, int]:
    """dns_check.py until its verdict is OK, bounded; a CHECK_FAILED is retried, never accepted.
    Each attempt prints one progress line (verdict and which part is not yet right, no addresses)."""
    verdict = "DNS_CHECK_FAILED"
    for attempt in range(1, attempts + 1):
        done = run([sys.executable, str(ROOT / "scripts/edge/dns_check.py"), "--domain", domain, *expect],
                   capture_output=True, text=True, timeout=120)
        try:
            last = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
        except ValueError:
            last = {}
        verdict = last.get("verdict", "DNS_CHECK_FAILED")
        out(json.dumps({"attempt": attempt, "of": attempts, "verdict": verdict, "A_status": last.get("A_status"),
                        "AAAA_status": last.get("AAAA_status"), "check_errors": last.get("check_errors"),
                        "duration_s": last.get("duration_s")}), flush=True)
        if done.returncode == 0:
            return verdict, attempt
        if attempt < attempts:
            sleep(interval)
    return verdict, attempts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--credentials", required=True, help="JSON credentials file outside the repository")
    parser.add_argument("--execute", action="store_true", help="really send the update (owner-approved)")
    parser.add_argument("--attempts", type=int, default=15, help="dns_check attempts after the update")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--endpoint", default=ENDPOINT, help=argparse.SUPPRESS)  # loopback test server only
    sub = parser.add_subparsers(dest="action", required=True)
    online = sub.add_parser("online")
    online.add_argument("--ip", required=True)
    offline = sub.add_parser("offline")
    offline.add_argument("--mode", choices=["no-ipv4", "signoff"], required=True)
    offline.add_argument("--offline-ip", help="owner-approved offline IPv4 (Dynu's offline server)")
    offline.add_argument("--offline-ipv6", help="owner-approved offline IPv6 (Dynu's offline server)")
    offline.add_argument("--released-ip", help="the LB address that must no longer be answered")
    args = parser.parse_args(argv)
    for flag, value, version in (("--ip", getattr(args, "ip", None), 4), ("--offline-ip", getattr(args, "offline_ip", None), 4),
                                 ("--offline-ipv6", getattr(args, "offline_ipv6", None), 6)):
        if value is not None:
            try:
                parsed = ipaddress.ip_address(value)
            except ValueError:
                raise SystemExit(f"{flag}: {value!r} is not an IP address")
            if parsed.version != version:
                raise SystemExit(f"{flag}: {value!r} is not an IPv{version} address")
    creds = load_credentials(args.credentials)
    host = creds["hostname"]
    query = params(args.action, host, getattr(args, "ip", None), getattr(args, "mode", None))
    shown = {k: v for k, v in query.items()}
    if not args.execute:
        print(json.dumps({"planned": f"GET {args.endpoint}", "query": shown, "sent": False}))
        return 0
    code = send(creds, query, args.endpoint)
    print(json.dumps({"query": shown, "dynu_response": code,
                      "update_status": "accepted (DNS decides)" if code in OK_CODES else "rejected"}), flush=True)
    if code not in OK_CODES:
        print(json.dumps({"verdict": "DNS_UPDATE_FAILED", "reason": FATAL_HINTS.get(code, code)}))
        return 2
    expect = ["--expect-ip", args.ip] if args.action == "online" else ["--expect-offline", *(
        ["--offline-ip", args.offline_ip] if args.offline_ip else []), *(
        ["--offline-ipv6", args.offline_ipv6] if args.offline_ipv6 else []), *(
        ["--released-ip", args.released_ip] if args.released_ip else [])]
    try:
        verdict, attempts = verify(host, expect, args.attempts, args.interval)
    except KeyboardInterrupt:
        print(json.dumps({"verdict": "INTERRUPTED", "note": "verification stopped; no further DNS update was sent"}), flush=True)
        return 130
    print(json.dumps({"verdict": verdict, "dns_check_attempts": attempts}))
    return 0 if verdict.endswith("_OK") else (1 if verdict.endswith("NOT_YET") else 2)


if __name__ == "__main__":
    raise SystemExit(main())
