"""DNS state of the public edge hostnames, at the authoritative servers and public resolvers.

  online:  every host answers exactly --expect-ip (the LB address) and has no AAAA record
  offline: no host answers an A or AAAA record (or only an owner-approved --offline-ip)

Checks the four edge names, the bare domain and a random name (the Dynu wildcard alias). A
value that is not the expected one is reported as "unexpected (withheld)": it may be a home
address and must not reach the evidence. Read-only (dig).

  python3 scripts/edge/dns_check.py --domain ewallet-hg.gleeze.com --expect-ip <LB IP>
  python3 scripts/edge/dns_check.py --domain ewallet-hg.gleeze.com --expect-offline
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import secrets
import subprocess
from typing import Callable

HOSTS = ("features", "agents", "registry", "observe")


def dig(server: str, name: str, rtype: str, run: Callable = subprocess.run) -> list[str]:
    done = run(["dig", f"@{server}", "+short", "+time=3", "+tries=2", name, rtype], capture_output=True, text=True)
    return [line.strip() for line in done.stdout.splitlines() if line.strip() and not line.startswith(";")]


def authoritative(domain: str, run: Callable = subprocess.run) -> list[str]:
    """The NS of the closest enclosing zone (the domain itself, else its parents)."""
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        done = run(["dig", "+short", "NS", ".".join(labels[i:])], capture_output=True, text=True)
        servers = [s.strip().rstrip(".") for s in done.stdout.splitlines() if s.strip()]
        if servers:
            return servers
    return []


def describe(values: list[str], expected: str | None) -> str:
    return "none" if not values else ", ".join(v if v == expected else "unexpected (withheld)" for v in values)


def check(domain: str, expect_ip: str | None, offline_ip: str | None, servers: list[str], run: Callable = subprocess.run) -> list[dict]:
    names = [domain, *(f"{h}.{domain}" for h in HOSTS), f"wildcard-probe-{secrets.token_hex(4)}.{domain}"]
    results = []
    for server in servers:
        for name in names:
            a, aaaa = dig(server, name, "A", run), dig(server, name, "AAAA", run)
            a = [v for v in a if _is_ip(v)]  # drop CNAME lines; the target address is what counts
            if expect_ip:
                ok = a == [expect_ip] and not aaaa
            else:
                ok = (not a or a == [offline_ip]) and not aaaa
            label = name if not name.startswith("wildcard-probe-") else f"<random>.{domain}"
            results.append({"server": server, "name": label, "passed": ok,
                            "A": describe(a, expect_ip or offline_ip), "AAAA": describe(aaaa, None)})
    return results


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--expect-ip", help="the LB address every name must resolve to")
    mode.add_argument("--expect-offline", action="store_true", help="no name may resolve (cleanup check)")
    parser.add_argument("--offline-ip", help="an owner-approved placeholder that counts as offline")
    parser.add_argument("--resolver", action="append", default=[], help="public resolver (default 1.1.1.1 and 8.8.8.8)")
    args = parser.parse_args(argv)
    if args.expect_ip:
        ipaddress.ip_address(args.expect_ip)
    servers = authoritative(args.domain) + (args.resolver or ["1.1.1.1", "8.8.8.8"])
    results = check(args.domain, args.expect_ip, args.offline_ip, servers)
    for r in results:
        print(json.dumps(r))
    ok = bool(results) and all(r["passed"] for r in results)
    print("DNS_" + ("ONLINE" if args.expect_ip else "OFFLINE") + ("_OK" if ok else "_NOT_YET"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
