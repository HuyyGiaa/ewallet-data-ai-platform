"""Refuse Kubernetes manifests that would select a retired MCP credential.

The legacy Specialist MCP credentials were revoked server-side (closure-20261116T211833Z): the final server sets
(``fin-*``) no longer contain them. The older server sets ``legacy`` (unsuffixed ``mcp-auth-<server>``),
``r1-transition`` and ``sbx-20261115`` did contain them, and older Helm revisions of ``llm-agent-mcp`` (<= 17) and
``llm-agent`` (<= 57) select those sets. Re-applying such a manifest (a historical ``helm rollback``, an old
profile, a deploy with stale values) would make a server trust a retired credential again, or (once the old auth
tables are deleted) leave it unable to start. Every project script that renders, upgrades or rolls back an
agent-platform release runs this check on the exact manifest first and refuses on any finding.

Retired references (exact Secret names, matched as whole tokens anywhere in the manifest):
  mcp-auth-<server>                         the ``legacy`` server set (no set suffix)
  mcp-auth-<server>-r1-transition           a retired transition set
  mcp-auth-<server>-sbx-20261115            the last set that still held the Specialist credentials
  mcp-token-drift-specialist-*              retired Specialist client credentials (Secrets deleted)
  mcp-token-feature-rag-specialist-*

It checks rendered manifests, not values files: a values file may still carry the chart's base name
``authSecret.name: mcp-auth-<server>`` that the chart suffixes with the active server set.

usage: python -m ops.llm_agent.retired_credentials check <manifest|-> [--label NAME]
Exit 0 when nothing retired is referenced, 1 with one line per finding otherwise.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import sys
from pathlib import Path

RETIRED_SERVER_SETS = ("legacy", "r1-transition", "sbx-20261115")
RETIRED_CLIENT_SUBJECTS = ("drift-specialist", "feature-rag-specialist")
_TOKEN = re.compile(r"(?<![A-Za-z0-9-])mcp-(?:auth|token)-[a-z0-9-]+(?![A-Za-z0-9-])")


@dataclass(frozen=True)
class Finding:
    name: str
    reason: str


def classify(name: str) -> str | None:
    """Why this Secret name is retired, or None when it is allowed."""

    if name.startswith("mcp-auth-"):
        rest = name[len("mcp-auth-"):]
        if rest.endswith("-mcp"):
            return "auth table of the retired 'legacy' server set"
        for server_set in RETIRED_SERVER_SETS[1:]:
            if rest.endswith(f"-mcp-{server_set}"):
                return f"auth table of the retired '{server_set}' server set"
        return None
    for subject in RETIRED_CLIENT_SUBJECTS:
        if name.startswith(f"mcp-token-{subject}-"):
            return f"retired {subject} client credential"
    return None


def findings(text: str) -> list[Finding]:
    seen: dict[str, Finding] = {}
    for match in _TOKEN.finditer(text):
        reason = classify(match.group(0))
        if reason and match.group(0) not in seen:
            seen[match.group(0)] = Finding(match.group(0), reason)
    return sorted(seen.values(), key=lambda f: f.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("manifest", help="rendered manifest file, or - for stdin")
    check.add_argument("--label", default="manifest")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.manifest == "-" else Path(args.manifest).read_text(encoding="utf-8")
    if not text.strip():
        print(f"retired_credentials: FAIL: {args.label} is empty (nothing to verify)", file=sys.stderr)
        return 1
    found = findings(text)
    for finding in found:
        print(f"retired_credentials: {args.label} references {finding.name} ({finding.reason})", file=sys.stderr)
    if found:
        print(f"retired_credentials: REFUSED {args.label}: it would select {len(found)} retired MCP credential "
              "object(s); fix forward from the current values instead", file=sys.stderr)
        return 1
    print(f"retired_credentials: {args.label}: no retired MCP credential referenced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
