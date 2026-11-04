"""Resolve an agent system prompt by explicit version from agentregistry.

The registry (helm release prompt-registry, namespace ewallet-prompts) is the
prompt management tool; Git (infra/helm/llm-agent-platform/prompts/manifest.json)
pins each version's sha256. Resolution is fail-closed:

* a version absent from the Git manifest is refused before any call;
* a version not published in the registry is an error, never a silent fallback;
* registry content whose sha256 differs from the manifest is rejected;
* only an unreachable registry may fall back to the pinned Git copy, and only
  when the caller allows it; the result records which source was used.

``configmap`` renders the ``system.md`` ConfigMap a kagent declarative agent
reads through ``systemMessageFrom``, annotated with the resolved version.

``gate`` (security assessment M3) is the mandatory fail-closed check around any
prompt publication or promotion: ``--phase pre`` passes only if every Git-pinned
version is either byte-identical in the registry or not yet published;
``--phase post`` passes only if every pinned version is byte-identical. ``configmap``
always runs the post gate first, so a tampered registry blocks promotion even of
an untouched version. ``registry_catalog publish`` runs both phases.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "infra/helm/llm-agent-platform/prompts"


class PromptRegistryError(RuntimeError):
    """The requested prompt version cannot be resolved safely."""


class PromptNotPublished(PromptRegistryError):
    """The version is pinned in Git but absent from the registry."""


class PromptTampered(PromptRegistryError):
    """Registry bytes differ from the Git-pinned sha256."""


class RegistryUnavailable(PromptRegistryError):
    """The registry could not be reached and no fallback was allowed."""


GATE_PHASES = ("pre", "post")


@dataclass(frozen=True)
class ResolvedPrompt:
    agent: str
    version: str
    sha256: str
    source: str
    text: str


def _registry_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise PromptRegistryError("registry must be an in-cluster or port-forwarded http:// URL")
    return url.rstrip("/")


def resolve(registry: str, agent: str, version: str, *, prompts_dir: Path = PROMPTS,
            opener: Callable[..., Any] = urlopen, allow_fallback: bool = False) -> ResolvedPrompt:
    manifest = json.loads((prompts_dir / "manifest.json").read_text(encoding="utf-8"))["prompts"]
    entry = manifest.get(f"{agent}/{version}")
    if entry is None:
        raise PromptRegistryError(f"{agent}/{version} is not in the Git prompt manifest")
    url = f"{_registry_url(registry)}/v0/prompts/{quote(agent)}-system/{quote(version)}"
    try:
        with opener(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
            text = json.loads(response.read())["spec"]["content"]
        source = "registry"
    except HTTPError as error:
        if error.code == 404:
            raise PromptNotPublished(f"{agent}/{version} is not published in the registry") from error
        if not allow_fallback:
            raise RegistryUnavailable(f"registry unavailable: HTTP {error.code}") from error
        text, source = (prompts_dir / entry["path"]).read_text(encoding="utf-8"), "pinned-git-fallback"
    except (URLError, OSError) as error:
        if not allow_fallback:
            raise RegistryUnavailable(f"registry unavailable: {error}") from error
        text, source = (prompts_dir / entry["path"]).read_text(encoding="utf-8"), "pinned-git-fallback"
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest != entry["sha256"]:
        raise PromptTampered(f"{agent}/{version} sha256 {digest} differs from the Git manifest {entry['sha256']}")
    return ResolvedPrompt(agent, version, digest, source, text)


def verify_all(registry: str, *, prompts_dir: Path = PROMPTS,
               opener: Callable[..., Any] = urlopen) -> dict[str, str]:
    """Detective control: every Git-pinned version must be published with its exact bytes."""

    manifest = json.loads((prompts_dir / "manifest.json").read_text(encoding="utf-8"))["prompts"]
    report = {}
    for key in sorted(manifest):
        agent, version = key.split("/", 1)
        try:
            resolve(registry, agent, version, prompts_dir=prompts_dir, opener=opener)
            report[key] = "MATCH"
        except PromptNotPublished as error:
            report[key] = f"NOT_PUBLISHED: {error}"
        except PromptTampered as error:
            report[key] = f"MISMATCH: {error}"
        except RegistryUnavailable as error:
            report[key] = f"UNAVAILABLE: {error}"
        except PromptRegistryError as error:
            report[key] = f"MISMATCH: {error}"
    return report


def gate(registry: str, phase: str, *, allow_tampered: bool = False, prompts_dir: Path = PROMPTS,
         opener: Callable[..., Any] = urlopen) -> tuple[bool, dict[str, str]]:
    """Fail-closed M3 gate. ``allow_tampered`` is only for a restore-from-Git publish."""

    if phase not in GATE_PHASES:
        raise ValueError(f"phase must be one of {GATE_PHASES}")
    report = verify_all(registry, prompts_dir=prompts_dir, opener=opener)
    allowed = {"MATCH"}
    if phase == "pre":
        allowed.add("NOT_PUBLISHED")
        if allow_tampered:
            allowed.add("MISMATCH")
    ok = all(value.split(":", 1)[0] in allowed for value in report.values())
    return ok, report


def configmap(prompt: ResolvedPrompt, *, name: str, namespace: str) -> str:
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/part-of": "ewallet-agents", "ewallet.io/agent": prompt.agent},
                     "annotations": {"ewallet.io/prompt-version": f"{prompt.agent}/{prompt.version}",
                                     "ewallet.io/prompt-sha256": prompt.sha256,
                                     "ewallet.io/prompt-source": prompt.source}},
        "data": {"system.md": prompt.text},
    }, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["resolve", "configmap", "verify-all", "gate"])
    parser.add_argument("--phase", choices=GATE_PHASES)
    parser.add_argument("--registry", default="http://127.0.0.1:12121")
    parser.add_argument("--agent")
    parser.add_argument("--version")
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--name")
    parser.add_argument("--namespace", default="ewallet-agents")
    args = parser.parse_args(argv)
    if args.command == "verify-all":
        report = verify_all(args.registry)
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0 if all(value == "MATCH" for value in report.values()) else 1
    if args.command == "gate":
        if not args.phase:
            parser.error("gate requires --phase pre|post")
        ok, report = gate(args.registry, args.phase)
        print(json.dumps({"phase": args.phase, "verdict": "PASS" if ok else "FAIL", "report": report},
                         indent=1, sort_keys=True))
        return 0 if ok else 1
    if args.command == "configmap":
        ok, report = gate(args.registry, "post")
        unavailable = all(value.startswith("UNAVAILABLE") for value in report.values())
        if not ok and not (args.allow_fallback and unavailable):
            print("prompt_registry: M3 gate failed; refusing to render a ConfigMap: "
                  + json.dumps({k: v for k, v in report.items() if v != "MATCH"}, sort_keys=True), file=sys.stderr)
            return 1
    if not (args.agent and args.version):
        parser.error("--agent and --version are required")
    try:
        prompt = resolve(args.registry, args.agent, args.version, allow_fallback=args.allow_fallback)
    except PromptRegistryError as error:
        print(f"prompt_registry: {error}", file=sys.stderr)
        return 1
    if args.command == "configmap":
        print(configmap(prompt, name=args.name or f"{args.agent}-prompt-managed", namespace=args.namespace), end="")
    else:
        print(json.dumps({"agent": prompt.agent, "version": prompt.version, "sha256": prompt.sha256,
                          "source": prompt.source, "bytes": len(prompt.text.encode())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
