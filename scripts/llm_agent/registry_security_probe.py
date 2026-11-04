"""Bounded security probe of an agentregistry 0.4.0 server (prompt management).

Read-only by default. It replaces the ad hoc probe that, on 2026-11-03, sent one
``POST /v0/apply?dryRun=false`` to the GKE registry and overwrote
``drift-specialist-system/v2`` (incident F9). Every request that could mutate the
registry goes through ``Probe.write``, which enforces:

* ``dryRun=true`` unless live writes were explicitly enabled with
  ``--allow-live-write``;
* a live write must name its intended target with ``--write-target NAME/TAG`` and
  the document must be exactly that target;
* a live write may never target a prompt pinned in the Git manifest, and its name
  must start with ``security-probe-`` (a throwaway object, never a real prompt).

The dry-run check reads the pinned prompt before and after and exits 2 if the
registry changed, so an accidental persistence is reported immediately.

usage:
  python scripts/llm_agent/registry_security_probe.py --registry http://127.0.0.1:12121 \
      --evidence out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "infra/helm/llm-agent-platform/prompts/manifest.json"
PROBE_PREFIX = "security-probe-"
READ_PATHS = ("/v0/agents", "/v0/mcpservers", "/v0/skills", "/v0/version", "/v0/health", "/openapi.json")


class ProbeSafetyError(RuntimeError):
    """A request would mutate the registry outside the explicitly allowed target."""


def pinned_prompt_names(manifest: Path = MANIFEST) -> set[str]:
    keys = json.loads(manifest.read_text(encoding="utf-8"))["prompts"]
    return {f"{key.split('/', 1)[0]}-system" for key in keys}


class Probe:
    def __init__(self, registry: str, *, allow_live_write: bool = False, write_target: str | None = None,
                 pinned: set[str] | None = None, opener: Callable[..., Any] = urlopen):
        parsed = urlparse(registry)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ProbeSafetyError("registry must be an explicit http:// URL")
        self.base = registry.rstrip("/")
        self.pinned = pinned_prompt_names() if pinned is None else pinned
        if allow_live_write:
            if not write_target or "/" not in write_target:
                raise ProbeSafetyError("--allow-live-write requires --write-target NAME/TAG")
            name = write_target.split("/", 1)[0]
            if name in self.pinned:
                raise ProbeSafetyError(f"{name} is a Git-pinned prompt; live writes to it are never allowed")
            if not name.startswith(PROBE_PREFIX):
                raise ProbeSafetyError(f"live write target must start with {PROBE_PREFIX!r}")
        elif write_target:
            raise ProbeSafetyError("--write-target is only meaningful with --allow-live-write")
        self.allow_live_write = allow_live_write
        self.write_target = write_target
        self.opener = opener
        self.writes: list[dict[str, Any]] = []

    def request(self, method: str, path: str, data: bytes | None = None,
                content_type: str = "application/json", headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        request = Request(self.base + path, data=data, method=method,
                          headers={"content-type": content_type, **(headers or {})})
        try:
            with self.opener(request, timeout=20) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def get(self, path: str) -> tuple[int, bytes]:
        return self.request("GET", path)

    def write(self, document: dict[str, Any], *, live: bool = False,
              headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        """The only mutating call. ``live`` must be requested here *and* enabled globally."""

        target = f"{document['metadata']['name']}/{document['metadata'].get('tag', '')}"
        if live:
            if not self.allow_live_write:
                raise ProbeSafetyError("live write requested but --allow-live-write was not given")
            if target != self.write_target:
                raise ProbeSafetyError(f"live write to {target} differs from --write-target {self.write_target}")
            if document["metadata"]["name"] in self.pinned:
                raise ProbeSafetyError(f"{target} is a Git-pinned prompt")
        query = "false" if live else "true"
        self.writes.append({"target": target, "dryRun": query})
        return self.request("POST", f"/v0/apply?dryRun={query}", yaml.safe_dump(document).encode(),
                            "application/yaml", headers)


def _content(probe: Probe, name: str, tag: str) -> str:
    status, body = probe.get(f"/v0/prompts/{name}/{tag}")
    if status != 200:
        raise RuntimeError(f"GET {name}/{tag} returned {status}")
    return json.loads(body)["spec"]["content"]


def run(probe: Probe, subject: str = "drift-specialist-system/v2") -> dict[str, Any]:
    name, tag = subject.split("/", 1)
    report: dict[str, Any] = {"registry": probe.base, "reads": {}, "dry_run_write": {}}
    for path in READ_PATHS:
        report["reads"][path] = probe.get(path)[0]
    before = _content(probe, name, tag)
    altered = {"apiVersion": "ar.dev/v1alpha1", "kind": "Prompt", "metadata": {"name": name, "tag": tag},
               "spec": {"description": "security probe (dry run)", "content": before + "\nprobe\n"}}
    status, _ = probe.write(altered)
    after = _content(probe, name, tag)
    report["dry_run_write"] = {"subject": subject, "status": status,
                               "unchanged": before == after,
                               "sha256_after": hashlib.sha256(after.encode()).hexdigest()}
    report["writes"] = probe.writes
    report["verdict"] = "PASS" if before == after else "INCIDENT: registry changed by a dry-run probe"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--subject", default="drift-specialist-system/v2")
    parser.add_argument("--allow-live-write", action="store_true")
    parser.add_argument("--write-target")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        probe = Probe(args.registry, allow_live_write=args.allow_live_write, write_target=args.write_target)
    except ProbeSafetyError as error:
        print(f"registry_security_probe: {error}", file=sys.stderr)
        return 1
    report = run(probe, args.subject)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.evidence:
        args.evidence.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
