"""DNS state of the public edge hostnames, verified at the authoritative servers and public resolvers.

  online:  every name answers exactly --expect-ip (the LB address) and has no AAAA record
  offline: each name answers no A record or only the owner-approved --offline-ip (IPv4), and no
           AAAA record or only the owner-approved --offline-ipv6 (IPv6); "no record" counts only
           as a proven NXDOMAIN or NOERROR/NODATA. --released-ip names the LB address that must
           be gone. (Dynu's offline mode answers its own offline server on both families.)

Fail-closed: a query counts only when dig exits 0, prints a parseable header with status
NOERROR or NXDOMAIN and, at an authoritative server, the "aa" flag. A timeout, SERVFAIL,
REFUSED, an unreachable server, a malformed reply or no authoritative servers make the whole
check DNS_CHECK_FAILED (exit 2): an unanswered question never counts as "no record".

Checks the four edge names, the bare domain and a random name (the wildcard alias). A value
that is not the expected one is reported as "unexpected (withheld)": it may be a home address.

  exit 0  DNS_ONLINE_OK / DNS_OFFLINE_OK
  exit 1  DNS_ONLINE_NOT_YET / DNS_OFFLINE_NOT_YET  (valid answers, wrong state: retry later)
  exit 2  DNS_CHECK_FAILED                          (the state could not be proven)

The last line is a summary: verdict, failed servers and names, A/AAAA status, duration.
Queries run concurrently (each dig: 3 s x 2 tries), so a check takes seconds, not minutes.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import ipaddress
import json
import re
import secrets
import subprocess
import time
from typing import Callable

HOSTS = ("features", "agents", "registry", "observe")
VALID_STATUS = ("NOERROR", "NXDOMAIN")


@dataclass
class Answer:
    """One query: a proven answer (error is None) or why it proves nothing."""

    status: str = ""
    authoritative: bool = False
    records: list[str] = field(default_factory=list)
    error: str | None = None


def query(server: str, name: str, rtype: str, authoritative: bool, run: Callable = subprocess.run) -> Answer:
    done = run(["dig", f"@{server}", name, rtype, "+noall", "+comments", "+answer", "+time=3", "+tries=2"],
               capture_output=True, text=True)
    text = done.stdout or ""
    if done.returncode != 0:
        reason = "timeout" if done.returncode == 9 else f"dig exit {done.returncode}"
        return Answer(error=reason)
    header = re.search(r"->>HEADER<<-.*status: ([A-Z]+)", text)
    flags = re.search(r";; flags: ([a-z ]+);", text)
    if not header or not flags:
        return Answer(error="malformed reply (no header)")
    answer = Answer(status=header.group(1), authoritative="aa" in flags.group(1).split())
    if answer.status not in VALID_STATUS:
        answer.error = answer.status  # SERVFAIL, REFUSED, ...
        return answer
    if authoritative and not answer.authoritative:
        answer.error = "not authoritative (aa flag missing)"
        return answer
    for line in text.splitlines():
        parts = line.split()
        if line.startswith(";") or len(parts) < 5:
            continue
        if parts[3] == rtype:
            try:
                answer.records.append(str(ipaddress.ip_address(parts[4])))
            except ValueError:
                answer.error = "malformed record"
                return answer
    return answer


def authoritative_servers(domain: str, resolver: str = "1.1.1.1", run: Callable = subprocess.run) -> tuple[list[str], str | None]:
    """The NS of the closest enclosing zone, asked of a public resolver with the same rules."""
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        zone = ".".join(labels[i:])
        done = run(["dig", f"@{resolver}", zone, "NS", "+noall", "+comments", "+answer", "+time=3", "+tries=2"],
                   capture_output=True, text=True)
        if done.returncode != 0:
            return [], f"NS lookup for {zone}: dig exit {done.returncode}"
        header = re.search(r"->>HEADER<<-.*status: ([A-Z]+)", done.stdout or "")
        if not header:
            return [], f"NS lookup for {zone}: malformed reply"
        if header.group(1) not in VALID_STATUS:
            return [], f"NS lookup for {zone}: {header.group(1)}"
        servers = [p.split()[4].rstrip(".") for p in done.stdout.splitlines()
                   if not p.startswith(";") and len(p.split()) >= 5 and p.split()[3] == "NS"]
        if servers:
            return servers, None
    return [], "no authoritative servers found"


def describe(values: list[str], expected: set[str]) -> str:
    return "none" if not values else ", ".join(v if v in expected else "unexpected (withheld)" for v in values)


def address(value: str | None, version: int, flag: str) -> str | None:
    """Canonical text of an address of the given family; anything else is an argument error."""
    if value is None:
        return None
    try:
        parsed = ipaddress.ip_address(value.strip())
    except ValueError:
        raise SystemExit(f"{flag}: {value!r} is not an IP address")
    if parsed.version != version:
        raise SystemExit(f"{flag}: {value!r} is not an IPv{version} address")
    return str(parsed)


def check(domain: str, expect_ip: str | None, offline_ip: str | None, released_ip: str | None,
          authoritative: list[str], resolvers: list[str], run: Callable = subprocess.run,
          offline_ipv6: str | None = None) -> tuple[str, list[dict]]:
    names = [domain, *(f"{h}.{domain}" for h in HOSTS), f"wildcard-probe-{secrets.token_hex(4)}.{domain}"]
    shown = {v for v in (expect_ip, offline_ip, offline_ipv6, released_ip) if v}
    targets = [(server, is_auth, name) for server, is_auth in [(s, True) for s in authoritative] + [(s, False) for s in resolvers]
               for name in names]
    with ThreadPoolExecutor(max_workers=16) as pool:
        answers = list(pool.map(lambda t: (query(t[0], t[2], "A", t[1], run), query(t[0], t[2], "AAAA", t[1], run)), targets))
    results, failed, ok_all = [], False, True
    for (server, is_auth, name), (a, aaaa) in zip(targets, answers):
        label = name if not name.startswith("wildcard-probe-") else f"<random>.{domain}"
        row = {"server": server, "authoritative": is_auth, "name": label}
        errors = ([f"A: {a.error}"] if a.error else []) + ([f"AAAA: {aaaa.error}"] if aaaa.error else [])
        if errors:
            failed = True
            row.update(passed=False, state="CHECK_FAILED", errors=errors)
        else:
            if expect_ip:
                # online: exactly the LB address, and no AAAA at all (no IPv6 LB is approved)
                a_ok, aaaa_ok = a.records == [expect_ip], not aaaa.records
            else:
                a_ok = not a.records or a.records == [offline_ip]
                aaaa_ok = not aaaa.records or aaaa.records == [offline_ipv6]
                if released_ip and released_ip in a.records + aaaa.records:
                    a_ok = aaaa_ok = False
            ok = a_ok and aaaa_ok
            ok_all &= ok
            row.update(passed=ok, state="OK" if ok else "NOT_YET", status=a.status, A_ok=a_ok, AAAA_ok=aaaa_ok,
                       A=describe(a.records, shown), AAAA=describe(aaaa.records, shown))
        results.append(row)
    mode = "ONLINE" if expect_ip else "OFFLINE"
    if failed or not results:
        return "DNS_CHECK_FAILED", results
    return (f"DNS_{mode}_OK" if ok_all else f"DNS_{mode}_NOT_YET"), results


def summary(verdict: str, rows: list[dict], seconds: float) -> dict:
    bad = [r for r in rows if not r.get("passed")]
    return {"verdict": verdict, "failed_servers": sorted({r["server"] for r in bad}),
            "failed_hostnames": sorted({r["name"] for r in bad}),
            "A_status": "OK" if all(r.get("A_ok") for r in rows if "A_ok" in r) and rows else "NOT_OK",
            "AAAA_status": "OK" if all(r.get("AAAA_ok") for r in rows if "AAAA_ok" in r) and rows else "NOT_OK",
            "check_errors": sum(1 for r in rows if r.get("state") == "CHECK_FAILED"), "duration_s": round(seconds, 1)}


def main(argv: list[str] | None = None, run: Callable = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--expect-ip", help="the LB address every name must resolve to")
    mode.add_argument("--expect-offline", action="store_true", help="no name may resolve (cleanup check)")
    parser.add_argument("--offline-ip", help="offline mode: the owner-approved offline IPv4 (e.g. Dynu's offline server)")
    parser.add_argument("--offline-ipv6", help="offline mode: the owner-approved offline IPv6")
    parser.add_argument("--released-ip", help="offline mode: the LB address that must no longer be answered")
    parser.add_argument("--resolver", action="append", default=[], help="public resolver (default 1.1.1.1 and 8.8.8.8)")
    parser.add_argument("--out", help="also write the JSON lines to this file")
    args = parser.parse_args(argv)
    started = time.monotonic()
    expect_ip = address(args.expect_ip, 4, "--expect-ip")
    offline_ip = address(args.offline_ip, 4, "--offline-ip")
    offline_ipv6 = address(args.offline_ipv6, 6, "--offline-ipv6")
    released = None
    if args.released_ip:
        try:
            released = str(ipaddress.ip_address(args.released_ip.strip()))
        except ValueError:
            raise SystemExit(f"--released-ip: {args.released_ip!r} is not an IP address")
    if expect_ip and (offline_ip or offline_ipv6):
        raise SystemExit("--offline-ip/--offline-ipv6 apply to --expect-offline only")
    resolvers = args.resolver or ["1.1.1.1", "8.8.8.8"]
    servers, error = authoritative_servers(args.domain, resolvers[0], run)
    if error:
        verdict, results = "DNS_CHECK_FAILED", [{"server": "-", "name": args.domain, "passed": False, "state": "CHECK_FAILED", "errors": [error]}]
    else:
        verdict, results = check(args.domain, expect_ip, offline_ip, released, servers, resolvers, run, offline_ipv6)
    lines = [json.dumps(r) for r in results] + [json.dumps(summary(verdict, results, time.monotonic() - started))]
    print("\n".join(lines))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return {"DNS_ONLINE_OK": 0, "DNS_OFFLINE_OK": 0, "DNS_ONLINE_NOT_YET": 1, "DNS_OFFLINE_NOT_YET": 1}.get(verdict, 2)


if __name__ == "__main__":
    raise SystemExit(main())
