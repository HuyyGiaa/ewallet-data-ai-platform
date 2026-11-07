"""Security checks against a running edge gateway, over real HTTPS.

Every check sends real traffic through curl with the route's own SNI and Host. ``--stub``
adds the checks that need a backend reporting what it received; the rest run unchanged on
GKE. The password file holds ``user:password`` and is never printed. A failed check fails
the run, so a report cannot show a partial pass as success.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import time
from typing import Sequence


@dataclass
class Result:
    """One check and what was observed."""

    name: str
    passed: bool
    observed: str


class Edge:
    """Send requests to one gateway address."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.address, self.https, self.http, self.domain = args.address, args.https_port, args.http_port, args.domain
        self.ca = args.ca_file
        self.credentials = Path(args.credentials_file).read_text(encoding="utf-8").strip()

    def curl(self, host: str, path: str, *extra: str, scheme: str = "https", port: int | None = None) -> tuple[int, str]:
        """Return status and body; a TLS or connection failure is status 0."""
        port = port or (self.https if scheme == "https" else self.http)
        name = f"{host}.{self.domain}"
        command = ["curl", "-sS", "-m", "20", "-o", "-", "-w", "\n%{http_code}", "--resolve", f"{name}:{port}:{self.address}",
                   f"{scheme}://{name}:{port}{path}"]
        if self.ca and scheme == "https":
            command += ["--cacert", self.ca]
        done = subprocess.run([*command, *extra], capture_output=True, text=True)
        body, _, status = done.stdout.rpartition("\n")
        return (int(status) if status.isdigit() else 0), body

    def authed(self, host: str, path: str, *extra: str) -> tuple[int, str]:
        """Send a request with valid credentials, kept out of the process arguments."""
        config = f'user = "{self.credentials}"\n'
        done = subprocess.run(
            ["curl", "-sS", "-m", "20", "-K", "-", "-o", "-", "-w", "\n%{http_code}", "--resolve",
             f"{host}.{self.domain}:{self.https}:{self.address}", f"https://{host}.{self.domain}:{self.https}{path}",
             *(["--cacert", self.ca] if self.ca else []), *extra],
            input=config, capture_output=True, text=True,
        )
        body, _, status = done.stdout.rpartition("\n")
        return (int(status) if status.isdigit() else 0), body


POST_JSON = ("-X", "POST", "-H", "content-type: application/json", "-d", "{}")
LOOKUP = "/v1/features/lookup"
COORDINATOR = "/api/a2a/ewallet-agents/fraud-coordinator"


def check(results: list[Result], name: str, passed: bool, observed: object) -> None:
    """Record one outcome."""
    results.append(Result(name, bool(passed), str(observed)[:160]))


def run_auth(edge: Edge, results: list[Result]) -> None:
    """Credentials: absent, wrong, forged and valid."""
    status, _ = edge.curl("features", LOOKUP, *POST_JSON)
    check(results, "missing credentials are rejected", status == 401, status)
    headers = subprocess.run(
        ["curl", "-sS", "-m", "20", "-D", "-", "-o", "/dev/null", "--resolve", f"features.{edge.domain}:{edge.https}:{edge.address}",
         f"https://features.{edge.domain}:{edge.https}{LOOKUP}", "-X", "POST", *(["--cacert", edge.ca] if edge.ca else [])],
        capture_output=True, text=True).stdout.lower()
    check(results, "401 tells a browser to ask for a password", "www-authenticate: basic" in headers, headers.split("\n")[0])
    status, _ = edge.curl("features", LOOKUP, "-u", "edge:definitely-wrong", *POST_JSON)
    check(results, "wrong password is rejected", status == 401, status)
    status, _ = edge.curl("features", LOOKUP, "-H", "x-edge-user: admin", "-H", "x-forwarded-user: admin",
                          "-H", "x-user-id: admin", *POST_JSON)
    check(results, "identity headers cannot replace credentials", status == 401, status)
    status, _ = edge.authed("features", LOOKUP, *POST_JSON)
    check(results, "valid credentials pass authentication", status not in (0, 401, 403, 404), status)


def run_routing(edge: Edge, results: list[Result]) -> None:
    """Only the declared hosts and paths exist."""
    status, _ = edge.curl("features", LOOKUP, scheme="http")
    check(results, "plain HTTP redirects to HTTPS", status == 301, status)
    for path in ("/metrics", "/internal/v1/entity-timestamps", "/docs", "/health/ready", "/openapi.json"):
        status, _ = edge.authed("features", path)
        check(results, f"undeclared path {path} is not routed", status == 404, status)
    status, _ = edge.authed("features", LOOKUP)
    check(results, "wrong method on a declared path is not routed", status == 404, status)
    for path in ("/api/agents", "/api/modelconfigs", "/api/sessions", f"{COORDINATOR}-other", "/api/a2a/ewallet-agents/drift-specialist"):
        status, _ = edge.authed("a2a", path)
        check(results, f"controller path {path} is not routed", status == 404, status)
    for host in ("vllm", "mlflow", "jenkins", "vault", "redis", "mcp", "kserve", "prometheus", "unknown"):
        status, _ = edge.curl(host, "/")
        check(results, f"host {host} is not served", status == 0 or status == 404, status)


def run_rate_limit(edge: Edge, results: list[Result]) -> None:
    """A burst on one route is limited, and another route keeps working."""
    with ThreadPoolExecutor(30) as pool:
        codes = list(pool.map(lambda _: edge.authed("a2a", COORDINATOR, "-X", "POST", "-d", "{}")[0], range(60)))
    limited = codes.count(429)
    check(results, "a burst receives HTTP 429", limited > 0, f"429={limited} of {len(codes)}")
    status, _ = edge.authed("features", LOOKUP, *POST_JSON)
    check(results, "the limit is per route", status != 429, status)
    time.sleep(2)
    status, _ = edge.authed("a2a", COORDINATOR, "-X", "POST", "-d", "{}")
    check(results, "the route recovers after the burst", status != 429, status)


def run_stub(edge: Edge, results: list[Result]) -> None:
    """Checks that read what the stub backend received."""
    status, body = edge.authed("a2a", COORDINATOR, "-X", "POST", "-H", "x-user-id: attacker", "-H", "x-edge-user: root", "-d", "{}")
    seen = json.loads(body).get("headers", {}) if status == 200 else {}
    check(results, "the upstream never sees the password", "authorization" not in seen, sorted(seen)[:8])
    check(results, "a client cannot choose the agent user id", seen.get("x-user-id") == "edge-a2a-client", seen.get("x-user-id"))
    check(results, "a forged edge user is removed", "x-edge-user" not in seen, seen.get("x-edge-user"))
    config = f'user = "{edge.credentials}"\n'
    name = f"agents.{edge.domain}"
    started = time.perf_counter()
    process = subprocess.Popen(
        ["curl", "-sSN", "-m", "20", "-K", "-", "--resolve", f"{name}:{edge.https}:{edge.address}",
         f"https://{name}:{edge.https}/stream", *(["--cacert", edge.ca] if edge.ca else [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    process.stdin.write(config)
    process.stdin.close()
    arrivals = []
    for line in process.stdout:
        if line.startswith("data:"):
            arrivals.append(round(time.perf_counter() - started, 2))
    process.wait()
    spread = arrivals[-1] - arrivals[0] if len(arrivals) > 1 else 0
    check(results, "SSE arrives event by event, not buffered", len(arrivals) == 4 and spread >= 1.5, arrivals)
    status, _ = edge.authed("features", "/v1/features/lookup", "-X", "POST", "-H", "x-stub-status: 503", "-d", "{}")
    _, counts = edge.authed("agents", "/calls")
    check(results, "the gateway does not retry a failed call", status == 503 and json.loads(counts).get("503", 0) == 1, counts)


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--stub", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    edge = Edge(args)
    results: list[Result] = []
    # Without this, "host is not served" would pass against a dead gateway.
    if edge.curl("features", LOOKUP, "-X", "POST")[0] == 0:
        print("FAIL the gateway did not answer; no check was run")
        return 2
    run_auth(edge, results)
    run_routing(edge, results)
    if args.stub:
        run_stub(edge, results)
    run_rate_limit(edge, results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(asdict(item)) + "\n" for item in results), encoding="utf-8")
    failed = [item for item in results if not item.passed]
    for item in failed:
        print(f"FAIL {item.name}: {item.observed}")
    print(f"{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
