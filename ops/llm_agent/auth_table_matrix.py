"""KEEP / DELETE / UNKNOWN matrix for MCP server auth tables (``mcp-auth-*`` Secrets).

Input is a token-free inventory collected by scripts/llm_agent/mcp_auth_tables.sh (credential ids, subjects and
statuses only; values never leave the collector) plus every place a table name is referenced: live objects,
the current Helm release manifests and every historical Helm revision.

A table is DELETE only when all of these hold (owner directive, Stage B2):
  1. its server set is one of the explicitly obsolete sets (``legacy`` = unsuffixed name, ``r1-transition``,
     ``sbx-20261115``);
  2. no live workload references it (pods, Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, scaled-up
     ReplicaSets, kagent/agent-sandbox resources); a ReplicaSet scaled to zero is rollout history, recorded as an
     unsafe ``kubectl rollout undo`` target and not a blocker;
  3. no current Helm release manifest references it;
  4. the server it belongs to is either absent (retired: nothing can read it) or reads a verified final table;
  5. every verified final table (the tables the live servers read) is present, immutable, has at least one
     active credential and no retired subject;
  6. it is in the reviewed deletion list (``--expect-delete``); any difference between the computed and the
     reviewed list fails the plan.
Protected tables are never deleted: the final sets ``fin-20261117`` and ``fin-20261117-b``, except where
NAMESPACE_OBSOLETE_SETS makes a set obsolete in one namespace only. A table matching no rule is UNKNOWN and kept.

Namespace-obsolete sets (offline audit lf-offline-20261117, docs/edai2/agents/AGENT_PLATFORM_COMPLETION.md §13): in
``ewallet-agents`` the final-set copies ``fin-20261117`` (llm-agent@56 only, the failed cr-agents attempt) and
``fin-20261117-b`` (llm-agent@58 only) served the in-release drift-mcp, retired in llm-agent@59. The same names stay
live and protected in ``ewallet-mcp``. Such a table is DELETE only when, in addition to rules 4-6: no live object in
its namespace names it, no current Helm manifest object in its namespace names it (the namespace-scoped view; a
same-named table read in another namespace does not count, an inventory without the scoped view blocks), and its
in-release server in that namespace is absent. Its retired-server rollback revisions are listed, not blockers.

usage: python -m ops.llm_agent.auth_table_matrix --inventory inventory.json --out matrix.json
           [--expect-delete ewallet-mcp/mcp-auth-drift-mcp-sbx-20261115 ...]
Exit 0 when the matrix is consistent (and equals the reviewed list when given), 1 otherwise.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sys
from typing import Any

OBSOLETE_SETS = ("legacy", "r1-transition", "sbx-20261115")
PROTECTED_SETS = ("fin-20261117", "fin-20261117-b")
NAMESPACE_OBSOLETE_SETS = {"ewallet-agents": ("fin-20261117", "fin-20261117-b")}
RETIRED_SUBJECTS = frozenset({"drift-specialist", "feature-rag-specialist"})
SERVERS = ("drift-mcp", "feature-rag-mcp")
_NAME = re.compile(r"^mcp-auth-(?P<server>" + "|".join(SERVERS) + r")(?:-(?P<set>[a-z0-9]([-a-z0-9]{0,22}[a-z0-9])?))?$")


@dataclass
class Row:
    namespace: str
    name: str
    server: str | None
    server_set: str | None
    decision: str
    reasons: list[str] = field(default_factory=list)
    live_references: list[str] = field(default_factory=list)
    history_references: list[str] = field(default_factory=list)
    current_helm_references: list[str] = field(default_factory=list)
    historical_helm_revisions: list[str] = field(default_factory=list)
    retired_subjects: list[str] = field(default_factory=list)
    credential_ids: list[str] = field(default_factory=list)


def parse_name(name: str) -> tuple[str | None, str | None]:
    match = _NAME.match(name)
    if not match:
        return None, None
    return match.group("server"), match.group("set") or "legacy"


def verify_final(table: dict[str, Any]) -> list[str]:
    """Problems with a table a live server reads (empty list = verified)."""

    problems = []
    credentials = table.get("credentials")
    if not isinstance(credentials, list):
        return ["credentials unreadable"]
    if table.get("immutable") is not True:
        problems.append("not immutable")
    if not any(c.get("status") == "active" for c in credentials):
        problems.append("no active credential")
    retired = sorted({c.get("subject") for c in credentials} & RETIRED_SUBJECTS)
    if retired:
        problems.append(f"holds retired subjects {retired}")
    return problems


def build(inventory: dict[str, Any]) -> dict[str, Any]:
    tables = inventory["tables"]
    by_key = {f"{t['namespace']}/{t['name']}": t for t in tables}
    references = inventory.get("references", {})
    live, history = references.get("live", {}), references.get("history", {})
    current_helm, helm_history = references.get("helm_current", {}), references.get("helm_history", {})
    current_scoped = references.get("helm_current_scoped")   # None: collected before the scoped view existed
    servers = inventory.get("servers", {})   # "<ns>/<server>" -> {"present": bool, "table": name|None}
    problems: list[str] = []

    final_keys: set[str] = set()
    for key, server in sorted(servers.items()):
        if not server.get("present"):
            continue
        namespace = key.split("/", 1)[0]
        table_key = f"{namespace}/{server.get('table')}"
        if table_key not in by_key:
            problems.append(f"live server {key} reads {table_key}, which does not exist")
            continue
        final_keys.add(table_key)
        found = verify_final(by_key[table_key])
        if found:
            problems.append(f"final table {table_key} of live server {key}: {', '.join(found)}")
        _, server_set = parse_name(server.get("table") or "")
        if server_set in OBSOLETE_SETS:
            problems.append(f"live server {key} still reads the obsolete set {server_set}")

    rows = []
    for key, table in sorted(by_key.items()):
        server, server_set = parse_name(table["name"])
        row = Row(table["namespace"], table["name"], server, server_set, "UNKNOWN",
                  live_references=sorted(live.get(key, [])), history_references=sorted(history.get(key, [])),
                  current_helm_references=sorted(current_helm.get(key, [])),
                  historical_helm_revisions=sorted(helm_history.get(key, [])),
                  retired_subjects=sorted({c.get("subject") for c in table.get("credentials") or []} & RETIRED_SUBJECTS),
                  credential_ids=sorted(c.get("id", "?") for c in table.get("credentials") or []))
        server_state = servers.get(f"{table['namespace']}/{server}") if server else None
        namespace_obsolete = server_set in NAMESPACE_OBSOLETE_SETS.get(table["namespace"], ())
        if key in final_keys:
            row.decision, row.reasons = "KEEP", ["read by a live server (verified final table)"]
        elif namespace_obsolete:
            scoped = None if current_scoped is None else sorted(current_scoped.get(key, []))
            if row.live_references:
                row.reasons = ["referenced by a live workload"]
            elif scoped is None:
                row.reasons = ["inventory has no namespace-scoped Helm references; re-collect"]
            elif scoped:
                row.reasons = [f"referenced by a current Helm manifest object in {table['namespace']}: {scoped}"]
            elif server_state is None:
                row.reasons = [f"server {table['namespace']}/{server} was not inventoried"]
            elif server_state.get("present"):
                row.reasons = [f"its in-release server {table['namespace']}/{server} is still deployed"]
            else:
                row.decision = "DELETE"
                row.reasons = [f"set {server_set} is obsolete in {table['namespace']}: its in-release server is absent "
                               "(retired); no live or namespace-scoped current Helm reference"]
                if row.current_helm_references:
                    row.reasons.append("the same name is read in another namespace (not this table)")
                history_scoped = sorted((references.get("helm_history_scoped") or {}).get(key, []))
                if history_scoped:
                    row.reasons.append("only historical revisions recreating the retired server name it: "
                                       + ",".join(history_scoped))
        elif server_set in PROTECTED_SETS:
            row.decision, row.reasons = "KEEP", [f"protected final server set {server_set}"]
        elif server is None:
            row.reasons = ["not a recognised MCP auth table name"]
        elif server_set not in OBSOLETE_SETS:
            row.reasons = [f"server set {server_set} is not an explicitly obsolete set"]
        elif row.live_references:
            row.reasons = ["referenced by a live workload"]
        elif row.current_helm_references:
            row.reasons = ["referenced by a current Helm release manifest"]
        elif server_state is None:
            row.reasons = [f"server {table['namespace']}/{server} was not inventoried"]
        elif server_state.get("present") and f"{table['namespace']}/{server_state.get('table')}" not in final_keys:
            row.reasons = ["its server is live without a verified final table"]
        else:
            row.decision = "DELETE"
            row.reasons = [f"obsolete set {server_set}; no live or current Helm reference; server "
                           + ("reads a verified final table" if server_state.get("present") else "is absent (retired)")]
            if row.history_references:
                row.reasons.append("only rollout history (zero-replica ReplicaSets) references it")
        rows.append(row)
    if problems:   # an unverified final table blocks every deletion
        for row in rows:
            if row.decision == "DELETE":
                row.decision, row.reasons = "UNKNOWN", ["final tables not verified: " + "; ".join(problems)]
    return {"problems": problems, "rows": [asdict(r) for r in rows],
            "delete": sorted(f"{r.namespace}/{r.name}" for r in rows if r.decision == "DELETE")}


def tsv(matrix: dict[str, Any]) -> str:
    lines = ["decision\tnamespace\tsecret\tserver\tset\tlive_refs\tcurrent_helm_refs\trollout_history_refs\t"
             "historical_helm_revisions\tretired_subjects\treasons"]
    for r in matrix["rows"]:
        lines.append("\t".join([r["decision"], r["namespace"], r["name"], str(r["server"]), str(r["server_set"]),
                                ",".join(r["live_references"]) or "-", ",".join(r["current_helm_references"]) or "-",
                                ",".join(r["history_references"]) or "-", ",".join(r["historical_helm_revisions"]) or "-",
                                ",".join(r["retired_subjects"]) or "-", "; ".join(r["reasons"])]))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expect-delete", nargs="*", default=None, metavar="NS/NAME")
    args = parser.parse_args(argv)
    matrix = build(json.loads(args.inventory.read_text(encoding="utf-8")))
    failures = list(matrix["problems"])
    if args.expect_delete is not None and sorted(args.expect_delete) != matrix["delete"]:
        failures.append(f"computed deletions {matrix['delete']} differ from the reviewed list {sorted(args.expect_delete)}")
    matrix["failures"] = failures
    args.out.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    args.out.with_suffix(".tsv").write_text(tsv(matrix), encoding="utf-8")
    sys.stdout.write(tsv(matrix))
    for failure in failures:
        print(f"auth_table_matrix: FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
