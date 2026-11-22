"""Security checks against a running edge gateway, over real HTTPS.

Every check sends real traffic through curl with the route's own SNI and Host. ``--stub``
adds the checks that need a backend reporting what it received; the rest run unchanged on
GKE. ``--foundation`` skips the Basic Auth and rate-limit checks, for a gateway whose
security filters are not rendered yet (G2.1). The password file holds ``user:password`` and
is never printed. A failed check fails the run, so a report cannot show a partial pass as
success.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import math
import subprocess
import tempfile
import time
from typing import Sequence
from urllib.parse import urlsplit
import uuid


@dataclass
class Result:
    """One check and what was observed."""

    name: str
    passed: bool
    observed: str


def curl_user_config(credentials: str) -> str:
    """A curl config line for ``user:password``. Inside double quotes curl unescapes
    backslash sequences, so a backslash or a quote in the password must be escaped, or curl
    would send a different password (or none). A line break cannot be expressed; refuse it."""
    if any(c in credentials for c in "\r\n") or ":" not in credentials:
        raise ValueError("credentials must be one user:password line")
    return 'user = "' + credentials.replace("\\", "\\\\").replace('"', '\\"') + '"\n'


@dataclass
class Reply:
    """What curl saw. A TLS or connection failure is status 0."""

    status: int
    body: str
    location: str  # where a redirect points, resolved by curl; empty otherwise


# The names on the edge certificate (temporary self-signed and Let's Encrypt alike).
CERTIFICATE_HOSTS = ("features", "agents", "registry", "observe")


class Edge:
    """Send requests to one gateway address."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.address, self.https, self.http, self.domain = args.address, args.https_port, args.http_port, args.domain
        self.ca, self.stub, self.foundation = args.ca_file, args.stub, args.foundation
        self.http2 = getattr(args, "rate_limit_mode", "threads") == "http2"
        self.config = curl_user_config(Path(args.credentials_file).read_text(encoding="utf-8").rstrip("\r\n"))

    def request(self, host: str, path: str, *extra: str, scheme: str = "https", authed: bool = False) -> Reply:
        """One request with the route's SNI and Host; credentials go through stdin.

        The certificate names only the four served hosts, so an HTTPS probe of any other host
        (a2a, vault, unknown, ...) connects with a certificate name (features) as SNI, keeps
        full certificate verification, and sends the probed host as Host / :authority. The
        edge routes by Host behind its single *.domain listener, so this is what decides
        whether that host is served (G3-B DNS transition 2026-11-22: probing with the host's
        own SNI failed the TLS check before any HTTP answer)."""
        port = self.https if scheme == "https" else self.http
        name = f"{host}.{self.domain}"
        connect = name if scheme != "https" or host in CERTIFICATE_HOSTS else f"features.{self.domain}"
        command = ["curl", "-sS", "-m", "20", "-o", "-", "-w", "\n%{redirect_url}\n%{http_code}",
                   "--resolve", f"{connect}:{port}:{self.address}", f"{scheme}://{connect}:{port}{path}"]
        if connect != name:
            command += ["-H", f"Host: {name}:{port}"]
        if self.ca and scheme == "https":
            command += ["--cacert", self.ca]
        if authed:
            command += ["-K", "-"]
        done = subprocess.run([*command, *extra], input=self.config if authed else "", capture_output=True, text=True)
        rest, _, status = done.stdout.rpartition("\n")
        body, _, location = rest.rpartition("\n")
        return Reply(int(status) if status.isdigit() else 0, body, location)

    def burst(self, host: str, path: str, count: int) -> tuple[list[Reply], float]:
        """``count`` authenticated requests as fast as the client can send them, and how long it
        took. Threads open one TLS connection each; through kubectl port-forward that tops out
        near 60 req/s. HTTP/2 multiplexes every request on one connection (about 200 req/s
        there, G3-A1), which is what reliably exceeds a token bucket."""
        started = time.perf_counter()
        if not self.http2:
            with ThreadPoolExecutor(30) as pool:
                replies = list(pool.map(lambda _: self.request(host, path, authed=True), range(count)))
            return replies, time.perf_counter() - started
        name = f"{host}.{self.domain}"
        with tempfile.TemporaryDirectory() as bodies:
            command = ["curl", "-s", "--http2", "-Z", "--parallel-immediate", "--parallel-max", str(count), "-m", "20",
                       "--resolve", f"{name}:{self.https}:{self.address}", "-K", "-",
                       *(["--cacert", self.ca] if self.ca else [])]
            # --write-out is global in curl: one format for every transfer, which names its own
            # output file, so each line maps back to its request.
            command += ["-w", "%{filename_effective} %{http_code} %{redirect_url}\n"]
            for i in range(count):
                command += ["-o", f"{bodies}/{i}", f"https://{name}:{self.https}{path}"]
            done = subprocess.run(command, input=self.config, capture_output=True, text=True)
            seconds = time.perf_counter() - started
            replies = [Reply(0, "", "")] * count  # a transfer curl never reports is a failure
            for line in done.stdout.splitlines():
                filename, status, *location = line.split(" ", 2) + [""]
                index = Path(filename).name
                if not index.isdigit() or int(index) >= count:
                    continue
                body = Path(filename).read_text(errors="replace") if Path(filename).exists() else ""
                replies[int(index)] = Reply(int(status) if status.isdigit() else 0, body, location[0].strip())
        return replies, seconds

    def curl(self, host: str, path: str, *extra: str, scheme: str = "https") -> tuple[int, str]:
        """Without credentials: status and body."""
        reply = self.request(host, path, *extra, scheme=scheme)
        return reply.status, reply.body

    def authed(self, host: str, path: str, *extra: str) -> tuple[int, str]:
        """With valid credentials: status and body."""
        reply = self.request(host, path, *extra, authed=True)
        return reply.status, reply.body


POST_JSON = ("-X", "POST", "-H", "content-type: application/json", "-d", "{}")
LOOKUP = "/v1/features/lookup"


def stub_echo(status: int, body: str) -> dict | None:
    """The headers the stub received, when the reply is the stub's 200 echo."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("headers"), dict):
        return None
    return payload["headers"]


def reached_application(edge: Edge, status: int, body: str) -> bool:
    """A real answer from the backend, not merely "not rejected": the stub's echo, or the
    Feature API's own validation error for the empty lookup body. A 5xx never counts."""
    if edge.stub:
        return stub_echo(status, body) is not None
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    return status == 422 and isinstance(error, dict) and bool(error.get("correlation_id"))


def registry_served(edge: Edge, reply: Reply) -> bool:
    """The registry answered through the edge. The stub must echo with 200. The real UI
    may answer 200, or redirect, but only to HTTPS on its own host; a redirect elsewhere,
    any other 3xx/4xx/5xx and a connection failure (0) do not count."""
    if edge.stub:
        return stub_echo(reply.status, reply.body) is not None
    if reply.status == 200:
        return True
    target = urlsplit(reply.location)
    return reply.status in (301, 302, 303, 307, 308) and target.scheme == "https" \
        and target.hostname == f"registry.{edge.domain}"


def check(results: list[Result], name: str, passed: bool, observed: object) -> None:
    """Record one outcome."""
    results.append(Result(name, bool(passed), str(observed)[:300]))


def run_auth(edge: Edge, results: list[Result]) -> None:
    """Credentials: absent, wrong, forged and valid."""
    status, _ = edge.curl("features", LOOKUP, *POST_JSON)
    check(results, "missing credentials are rejected", status == 401, status)
    headers = subprocess.run(
        ["curl", "-sS", "-m", "20", "-D", "-", "-o", "/dev/null", "--resolve", f"features.{edge.domain}:{edge.https}:{edge.address}",
         f"https://features.{edge.domain}:{edge.https}{LOOKUP}", "-X", "POST", *(["--cacert", edge.ca] if edge.ca else [])],
        capture_output=True, text=True).stdout.lower()
    check(results, "401 tells a browser to ask for a password", "www-authenticate: basic" in headers, headers.split("\n")[0])
    check(results, "the challenge names the realm", 'basic realm="e-wallet"' in headers, "realm present" if 'realm=' in headers else "no realm")
    status, _ = edge.curl("features", LOOKUP, "-u", "edge:definitely-wrong", *POST_JSON)
    check(results, "wrong password is rejected", status == 401, status)
    status, _ = edge.curl("features", LOOKUP, "-u", "nobody:definitely-wrong", *POST_JSON)
    check(results, "unknown user is rejected", status == 401, status)
    status, _ = edge.curl("features", LOOKUP, "-H", "authorization: Bearer forged", *POST_JSON)
    check(results, "a bearer token is not a credential", status == 401, status)
    for host, path in (("unknown", "/"), ("features", "/metrics"), ("agents", "/api/agents")):
        status, _ = edge.curl(host, path)
        # The agents host may also be refused by the source restriction (403, which runs before
        # Basic Auth) when this client is not an approved address, e.g. through a port-forward.
        refused = {401, 403} if host == "agents" else {401}
        check(results, f"unauthenticated {host}{path} is rejected before routing", status in refused, status)
    status, _ = edge.curl("features", LOOKUP, "-H", "x-edge-user: admin", "-H", "x-forwarded-user: admin",
                          "-H", "x-user-id: admin", *POST_JSON)
    check(results, "identity headers cannot replace credentials", status == 401, status)
    status, body = edge.authed("features", LOOKUP, *POST_JSON)
    check(results, "valid credentials reach the application", reached_application(edge, status, body), status)


def run_routing(edge: Edge, results: list[Result]) -> None:
    """Only the declared hosts and paths exist."""
    status, _ = edge.curl("features", LOOKUP, scheme="http")
    check(results, "plain HTTP redirects to HTTPS", status == 301, status)
    for path in ("/metrics", "/internal/v1/entity-timestamps", "/docs", "/health/ready", "/openapi.json"):
        status, _ = edge.authed("features", path)
        check(results, f"undeclared path {path} is not routed", status == 404, status)
    status, _ = edge.authed("features", LOOKUP)
    check(results, "wrong method on a declared path is not routed", status == 404, status)
    reply = edge.request("features", LOOKUP, scheme="http")
    target = urlsplit(reply.location)
    check(results, "the redirect points to HTTPS on the same host",
          target.scheme == "https" and target.hostname == f"features.{edge.domain}", reply.location)
    # Every host below sits under the listener's *.domain wildcard, so the edge answers it
    # itself: Basic Auth first (401; 404 before the filters exist), then routing (404). A
    # connection failure (0) would hide a dead gateway, so it does not count.
    expected_anonymous = 404 if edge.foundation else 401
    for host in ("a2a", "fraud", "vllm", "mlflow", "jenkins", "vault", "redis", "mcp", "kserve", "prometheus", "unknown"):
        anonymous, _ = edge.curl(host, "/")
        status, _ = edge.authed(host, "/")
        check(results, f"host {host} is not served", anonymous == expected_anonymous and status == 404,
              f"anonymous={anonymous} authenticated={status}")


# The registry route's bucket (values.yaml routes.agent-registry.rateLimit).
REGISTRY_BUCKET = {"max_tokens": 50, "tokens_per_fill": 50, "fill_seconds": 1.0}
BURST = 120


def rate_limit_verdict(served: int, limited: int, failed: int, unexpected: list[int], seconds: float,
                       bucket: dict = REGISTRY_BUCKET) -> tuple[str, str]:
    """PASS, FAIL or INCONCLUSIVE for one burst, from what was observed only.

    Local rate limiting counts per gateway pod; ``bucket`` is one pod's. ``capacity`` is the
    most tokens that pod can hand out during the burst (a full bucket plus every refill that
    can fall inside it), so more requests than that without a 429 proves the limit was not
    enforced; fewer proves nothing.
    """
    total = served + limited + failed + len(unexpected)
    capacity = bucket["max_tokens"] + bucket["tokens_per_fill"] * math.ceil(seconds / bucket["fill_seconds"])
    facts = (f"total={total} served={served} 429={limited} connection_failures={failed} unexpected={sorted(unexpected)} "
             f"seconds={seconds:.2f} rate={total / seconds if seconds else 0:.1f}/s capacity={capacity}")
    if unexpected:
        return "FAIL", "unexpected statuses (5xx, 401/403 with valid credentials, or a wrong redirect); " + facts
    if failed:
        return "INCONCLUSIVE", "connections failed before an answer (client or port-forward); " + facts
    if limited and served:
        return "PASS", facts
    if limited:
        return "INCONCLUSIVE", "no request was served, so the bucket may have been empty already; " + facts
    if total > capacity:
        return "FAIL", "more requests than the bucket can hold and no 429; " + facts
    return "INCONCLUSIVE", "the client did not send more requests than the bucket allows; " + facts


def run_rate_limit(edge: Edge, results: list[Result]) -> None:
    """A bounded burst on one route is limited, another route keeps working, and it recovers.

    120 requests on the registry route (50 per second, values.yaml). Local rate limiting
    counts per gateway pod. The verdict is PASS only with real answers and 429s and nothing
    else; a client too slow to exceed the bucket is INCONCLUSIVE, never PASS.
    """
    time.sleep(1.5)
    reply = edge.request("registry", "/", authed=True)
    check(results, "normal traffic on the limited route succeeds", registry_served(edge, reply), reply.status)
    replies, seconds = edge.burst("registry", "/", BURST)
    served = sum(registry_served(edge, r) for r in replies)
    limited = sum(r.status == 429 for r in replies)
    failed = sum(r.status == 0 for r in replies)
    unexpected = [r.status for r in replies if r.status not in (0, 429) and not registry_served(edge, r)]
    verdict, facts = rate_limit_verdict(served, limited, failed, unexpected, seconds)
    check(results, "a burst receives HTTP 429", verdict == "PASS",
          f"{verdict} ({'http2' if edge.http2 else 'threads'}) {facts}")
    with ThreadPoolExecutor(10) as pool:
        anonymous = list(pool.map(lambda _: edge.curl("registry", "/")[0], range(30)))
    # The limiter runs before Basic Auth, so an anonymous request ends in 401 or 429, nothing else.
    check(results, "an unauthenticated burst is refused with 401 or 429 only", set(anonymous) <= {401, 429},
          f"401={anonymous.count(401)} 429={anonymous.count(429)} other={sorted(set(anonymous) - {401, 429})}")
    status, body = edge.authed("features", LOOKUP, *POST_JSON)
    check(results, "the limit is per route: another route still reaches its backend", reached_application(edge, status, body), status)
    time.sleep(2)
    reply = edge.request("registry", "/", authed=True)
    check(results, "the route recovers after the burst: the backend answers again", registry_served(edge, reply), reply.status)


def run_stub(edge: Edge, results: list[Result]) -> None:
    """Checks that read what the stub backend received."""
    for host, path, extra in (("features", LOOKUP, POST_JSON), ("agents", "/", ()), ("registry", "/", ()), ("observe", "/", ())):
        status, body = edge.authed(host, path, *extra)
        check(results, f"declared route on {host} reaches the backend", stub_echo(status, body) is not None, status)
    status, body = edge.authed("features", LOOKUP, "-H", "x-user-id: attacker", "-H", "x-edge-user: root", *POST_JSON)
    seen = stub_echo(status, body)
    echoed = seen is not None
    seen = seen or {}
    check(results, "the upstream never sees the password", echoed and "authorization" not in seen, sorted(seen)[:8] or status)
    check(results, "a client-chosen user id is removed", echoed and "x-user-id" not in seen, seen.get("x-user-id", status))
    check(results, "a forged edge user is removed", echoed and "x-edge-user" not in seen, seen.get("x-edge-user", status))
    name = f"agents.{edge.domain}"
    started = time.perf_counter()
    process = subprocess.Popen(
        ["curl", "-sSN", "-m", "20", "-K", "-", "--resolve", f"{name}:{edge.https}:{edge.address}",
         f"https://{name}:{edge.https}/stream", *(["--cacert", edge.ca] if edge.ca else [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    process.stdin.write(edge.config)
    process.stdin.close()
    arrivals = []
    for line in process.stdout:
        if line.startswith("data:"):
            arrivals.append(round(time.perf_counter() - started, 2))
    process.wait()
    spread = arrivals[-1] - arrivals[0] if len(arrivals) > 1 else 0
    check(results, "SSE arrives event by event, not buffered", len(arrivals) == 4 and spread >= 1.5, arrivals)
    status, _ = edge.authed("observe", "/ws", "--http1.1", "-H", "connection: Upgrade", "-H", "upgrade: websocket",
                            "-H", "sec-websocket-version: 13", "-H", "sec-websocket-key: dGhlIHNhbXBsZSBub25jZQ==", "-m", "5")
    check(results, "a WebSocket upgrade passes the gateway", status == 101, status)
    # One client request must reach the backend exactly once, whatever the backend does.
    # The Feature API route has a 5 s timeout, so a 7 s backend delay must end in a 504.
    run = uuid.uuid4().hex[:8]
    for case, headers, expected in (("ok", (), {200}), ("503", ("-H", "x-stub-status: 503"), {503}),
                                    ("reset", ("-H", "x-stub-close: 1"), {502, 503}), ("timeout", ("-H", "x-stub-sleep: 7"), {504})):
        status, _ = edge.authed("features", LOOKUP, "-H", f"x-stub-key: {run}-{case}", *headers, *POST_JSON)
        counted, counts = edge.authed("agents", "/calls")
        try:
            attempts = json.loads(counts).get(f"{run}-{case}", 0)
        except ValueError:
            attempts = f"unread (/calls answered {counted})"
        check(results, f"no retry: {case} reaches the backend once", status in expected and attempts == 1,
              f"status={status} attempts={attempts}")


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
    parser.add_argument("--foundation", action="store_true", help="skip the Basic Auth and rate-limit checks")
    parser.add_argument("--rate-limit-mode", choices=["threads", "http2"], default="threads",
                        help="http2: one multiplexed connection (use through kubectl port-forward)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    edge = Edge(args)
    results: list[Result] = []
    # Without this, "host is not served" would pass against a dead gateway.
    if edge.curl("features", LOOKUP, "-X", "POST")[0] == 0:
        print("FAIL the gateway did not answer; no check was run")
        return 2
    if not args.foundation:
        run_auth(edge, results)
    run_routing(edge, results)
    if args.stub:
        run_stub(edge, results)
    if not args.foundation:
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
