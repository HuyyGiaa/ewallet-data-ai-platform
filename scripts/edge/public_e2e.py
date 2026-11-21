"""Replayable end-to-end checks through the edge gateway for rubric rows 37-43 (G3-B / G4).

Runs against any address of the gateway: the public LoadBalancer (G3-B/G4) or a port-forward
(G3-A style). Row 42 (Basic Auth and rate limiting) is negative_tests.py; this script covers
the rest and labels every result with --evidence-class (LOCAL_KIND, GKE_PRIVATE,
PUBLIC_INTERNET), which is never inferred.

Read-only on the backends. Grafana is reached by a form login: the edge strips Authorization,
so its API cannot use Basic Auth. Its credentials come from a file outside the repository
(``user:password``), as do the edge credentials. Nothing secret is printed.

  python3 scripts/edge/public_e2e.py --address <LB IP> --domain ewallet-hg.gleeze.com \
      --credentials-file ~/private/edge-cred --grafana-credentials-file ~/private/grafana-cred \
      --evidence-class PUBLIC_INTERNET --out ~/ewallet-evidence/gateway/<campaign>/e2e.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import negative_tests as nt  # noqa: E402  (the harness's curl client: SNI, Host, credentials on stdin)

ROOT = Path(__file__).resolve().parents[2]
HOSTS = ("features", "agents", "registry", "observe")


def row(results: list, rubric: str, name: str, passed: bool, observed: object) -> None:
    results.append({"row": rubric, "check": name, "passed": bool(passed), "observed": str(observed)[:300]})


def private_file(path: str) -> str:
    """Read a credential file that must live outside the repository."""
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise SystemExit(f"{resolved} is inside the repository; keep credentials outside it")
    return resolved.read_text(encoding="utf-8").rstrip("\r\n")


def tls_checks(edge: nt.Edge, results: list, run=subprocess.run) -> None:
    """Row 43: the certificate verifies (system trust unless --ca-file), names every host, and
    TLS 1.1 is refused."""
    base = ["openssl", "s_client", "-connect", f"{edge.address}:{edge.https}", "-servername", f"features.{edge.domain}"]
    trust = ["-CAfile", edge.ca] if edge.ca else []
    verify = run([*base, *trust, "-verify_return_error"], input="", capture_output=True, text=True).stdout
    row(results, "43", "certificate verifies", "Verify return code: 0 (ok)" in verify,
        next((l.strip() for l in verify.splitlines() if "Verify return code" in l), "no handshake"))
    cert = run([*base, *trust], input="", capture_output=True, text=True).stdout
    sans = run(["openssl", "x509", "-noout", "-ext", "subjectAltName"], input=cert, capture_output=True, text=True).stdout
    names = set(re.findall(r"DNS:([^,\s]+)", sans))
    wanted = {f"{h}.{edge.domain}" for h in HOSTS}
    row(results, "43", "certificate names the four hosts", wanted <= names, sorted(names))
    old = run([*base, "-tls1_1", "-cipher", "DEFAULT@SECLEVEL=0"], input="", capture_output=True, text=True).stdout
    row(results, "43", "TLS 1.1 refused", "Cipher is (NONE)" in old or "Cipher is" not in old,
        next((l.strip() for l in old.splitlines() if "Cipher is" in l), "no handshake"))


def feature_checks(edge: nt.Edge, results: list, lookup: str | None) -> None:
    """Row 43: the Feature API answers over HTTPS. An empty body proves the application was
    reached (422 + correlation_id); a supplied synthetic lookup is reported separately and only
    a 200 counts as BUSINESS_LOOKUP_SUCCESS."""
    status, body = edge.authed("features", nt.LOOKUP, *nt.POST_JSON)
    row(results, "43", "Feature API reached over HTTPS (APPLICATION_REACHED)", nt.reached_application(edge, status, body), status)
    if lookup:
        status, body = edge.authed("features", nt.LOOKUP, "-X", "POST", "-H", "content-type: application/json", "--data-binary", f"@{lookup}")
        code = (json.loads(body).get("error", {}) or {}).get("code") if body.startswith("{") else None
        row(results, "43", "synthetic lookup (BUSINESS_LOOKUP_SUCCESS only on 200)", status in (200, 422, 424),
            f"status={status} {'BUSINESS_LOOKUP_SUCCESS' if status == 200 else 'APPLICATION_REACHED code=' + str(code)}")


def ui_checks(edge: nt.Edge, results: list, rubric: str, host: str, api: str, expect: str = "allowed") -> None:
    """Rows 40 (agents) and 41 (registry): HTML, one static asset and a read-only API answer.
    For a restricted agents host seen from a non-allowed address, ``expect=denied`` wants 403."""
    status, html = edge.authed(host, "/")
    if expect == "denied":
        row(results, rubric, f"{host}: refused from a non-allowed address", status == 403, status)
        return
    row(results, rubric, f"{host}: UI page", status == 200 and "<html" in html.lower(), status)
    asset = re.search(r'(?:src|href)="(/_next/static/[^"]+\.(?:js|css))"', html)
    status_a, _ = edge.authed(host, asset.group(1)) if asset else (0, "")
    row(results, rubric, f"{host}: static asset", asset is not None and status_a == 200, f"{asset.group(1) if asset else 'none'} {status_a}")
    status_api, body = edge.authed(host, api)
    try:
        data = json.loads(body)
        count = len(data.get("data", data) if isinstance(data, dict) else data)
    except (ValueError, TypeError):
        count = None
    row(results, rubric, f"{host}: read-only API {api}", status_api == 200 and count is not None, f"status={status_api} items={count}")


def grafana_checks(edge: nt.Edge, results: list, credentials: str, root_url: str | None,
                   loki_query: str, tempo_limit: int = 5) -> None:
    """Rows 37-39 through observe.<domain>: login, dashboards, and real data from each source
    (not only a healthy datasource): Prometheus 'up' samples, Loki log lines, Tempo traces."""
    user, _, password = credentials.partition(":")
    with tempfile.TemporaryDirectory() as tmp:
        jar = ["-b", f"{tmp}/jar", "-c", f"{tmp}/jar"]
        login = Path(tmp, "login.json")
        login.write_text(json.dumps({"user": user, "password": password}))
        reply = edge.request("observe", "/", authed=True)
        row(results, "37", "Grafana redirects to its login on the same host", reply.status == 302 and (
            not urlsplit(reply.location).hostname or urlsplit(reply.location).hostname == f"observe.{edge.domain}"),
            f"{reply.status} {reply.location}")
        _, page = edge.authed("observe", "/login")
        app_url = (re.search(r'"appUrl":"([^"]*)"', page) or [None, None])[1]
        if root_url:
            row(results, "37", "Grafana appUrl is the public root URL", app_url == root_url, app_url)
        status, _ = edge.authed("observe", "/login", "-X", "POST", "-H", "content-type: application/json",
                                "--data-binary", f"@{login}", *jar)
        login.unlink()
        row(results, "37", "Grafana login", status == 200, status)
        status, body = edge.authed("observe", "/api/search?type=dash-db", *jar)
        dashboards = len(json.loads(body)) if status == 200 else 0
        row(results, "37", "Grafana dashboards listed", dashboards > 0, f"status={status} dashboards={dashboards}")
        status, body = edge.authed("observe", "/api/datasources/proxy/uid/prometheus/api/v1/query?query=up", *jar)
        samples = len(json.loads(body).get("data", {}).get("result", [])) if status == 200 else 0
        row(results, "37", "metrics: Prometheus returns 'up' samples", samples > 0, f"status={status} series={samples}")
        status, body = edge.authed("observe", "/api/datasources/proxy/uid/loki/loki/api/v1/query_range?limit=20&query="
                                   + quote(loki_query), *jar)
        lines = sum(len(s.get("values", [])) for s in json.loads(body).get("data", {}).get("result", [])) if status == 200 else 0
        row(results, "38", "logs: Loki returns log lines", lines > 0, f"status={status} lines={lines} query={loki_query}")
        status, body = edge.authed("observe", f"/api/datasources/proxy/uid/tempo/api/search?limit={tempo_limit}", *jar)
        traces = len(json.loads(body).get("traces", [])) if status == 200 else 0
        row(results, "39", "traces: Tempo returns traces", traces > 0, f"status={status} traces={traces}")
        edge.authed("observe", "/logout", *jar)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", required=True, help="gateway address (LB IP, or 127.0.0.1 with a port-forward)")
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--ca-file", help="trust only this CA (private tests); default: system trust")
    parser.add_argument("--credentials-file", required=True, help="edge Basic Auth user:password, outside the repo")
    parser.add_argument("--grafana-credentials-file", help="Grafana user:password, outside the repo (rows 37-39)")
    parser.add_argument("--grafana-root-url", help="expected Grafana appUrl, e.g. https://observe.<domain>/")
    parser.add_argument("--loki-query", default='{namespace="ewallet-agents"}')
    parser.add_argument("--lookup-json", help="optional synthetic lookup body (row 43 business lookup)")
    parser.add_argument("--agents", choices=["allowed", "denied", "skip"], default="allowed",
                        help="expected agents result from this client address")
    parser.add_argument("--evidence-class", required=True, choices=["LOCAL_KIND", "GKE_PRIVATE", "PUBLIC_INTERNET"])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    private_file(args.credentials_file)
    edge = nt.Edge(argparse.Namespace(address=args.address, https_port=args.https_port, http_port=args.http_port,
                                      domain=args.domain, ca_file=args.ca_file, stub=False, foundation=False,
                                      credentials_file=args.credentials_file))
    results: list = []
    tls_checks(edge, results)
    feature_checks(edge, results, args.lookup_json)
    if args.agents != "skip":
        ui_checks(edge, results, "40", "agents", "/api/agents", args.agents)
    ui_checks(edge, results, "41", "registry", "/v0/agents")
    if args.grafana_credentials_file:
        grafana_checks(edge, results, private_file(args.grafana_credentials_file), args.grafana_root_url, args.loki_query)
    for r in results:
        r["evidence_class"] = args.evidence_class
        print(json.dumps(r))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
