"""Exact object diffs for the sandbox watch packages (PKG-K kagent, PKG-KS kagent staging, PKG-W KEDA).

``identity`` proves that the pinned chart, the live values and (for kagent) the pilot
post-renderer reproduce the live release manifest object for object; otherwise an upgrade
would also change objects nobody reviewed. ``candidate`` proves the overlay changes exactly
the reviewed objects and fields, and nothing else.

    python -m ops.llm_agent.sandbox_watch_diff identity <live-manifest> <baseline-render>
    python -m ops.llm_agent.sandbox_watch_diff candidate <kagent|kagent-staging|keda> <baseline-render> <candidate-render>
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Mapping

import yaml

Key = tuple[str, str, str]
SANDBOX = "ewallet-sandbox"
AGENTS_STAGING, SANDBOX_STAGING = "ewallet-agents-staging", "ewallet-sandbox-staging"
_WRITER_GETTER = (("Role", "kagent-getter-role"), ("RoleBinding", "kagent-getter-rolebinding"),
                  ("Role", "kagent-writer-role"), ("RoleBinding", "kagent-writer-rolebinding"))
_ENV_SOURCES = (("Role", "kagent-ate-api-env-sources"), ("RoleBinding", "kagent-ate-api-env-sources"))

# Objects each overlay may add (all namespaced, in ewallet-sandbox) and, per changed object,
# the only leaf paths that may differ.
EXPECTED: dict[str, dict[str, Any]] = {
    "kagent": {
        "added": {
            ("Role", SANDBOX, "kagent-ate-api-env-sources"), ("RoleBinding", SANDBOX, "kagent-ate-api-env-sources"),
            ("Role", SANDBOX, "kagent-getter-role"), ("RoleBinding", SANDBOX, "kagent-getter-rolebinding"),
            ("Role", SANDBOX, "kagent-writer-role"), ("RoleBinding", SANDBOX, "kagent-writer-rolebinding"),
        },
        "changed": {
            ("ConfigMap", "kagent", "kagent-controller"): {"data.WATCH_NAMESPACES"},
            ("Deployment", "kagent", "kagent-controller"): {"spec.template.metadata.annotations.checksum/configmap"},
        },
        "values": {"ConfigMap/kagent-controller WATCH_NAMESPACES": "ewallet-agents,kagent-pilot,ewallet-sandbox"},
        "namespaces": {SANDBOX},
    },
    # PKG-KS (CI/CD V2-P3B), from the live PKG-K state: writer/getter RBAC in both staging namespaces, env-source
    # RBAC only in the staging sandbox (the post-renderer removes it from ewallet-agents-staging).
    "kagent-staging": {
        "added": {(kind, ns, name) for ns in (AGENTS_STAGING, SANDBOX_STAGING) for kind, name in _WRITER_GETTER}
                 | {(kind, SANDBOX_STAGING, name) for kind, name in _ENV_SOURCES},
        "changed": {
            ("ConfigMap", "kagent", "kagent-controller"): {"data.WATCH_NAMESPACES"},
            ("Deployment", "kagent", "kagent-controller"): {"spec.template.metadata.annotations.checksum/configmap"},
        },
        "values": {"ConfigMap/kagent-controller WATCH_NAMESPACES":
                   "ewallet-agents,kagent-pilot,ewallet-sandbox,ewallet-agents-staging,ewallet-sandbox-staging"},
        "namespaces": {AGENTS_STAGING, SANDBOX_STAGING},
    },
    "keda": {
        "added": {("RoleBinding", SANDBOX, "keda-operator")},
        "changed": {
            ("Deployment", "keda", name): {"spec.template.spec.containers[0].env[WATCH_NAMESPACE].value"}
            for name in ("keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks")
        },
        "values": {"WATCH_NAMESPACE": "ewallet-agents,ml-platform,ewallet-sandbox"},
        "namespaces": {SANDBOX},
    },
}


def load(text: str) -> dict[Key, dict]:
    objects: dict[Key, dict] = {}
    for document in yaml.safe_load_all(text):
        if not document:
            continue
        metadata = document.get("metadata") or {}
        key = (str(document.get("kind", "")), str(metadata.get("namespace", "")), str(metadata.get("name", "")))
        if key in objects:
            raise ValueError(f"duplicate object {key}")
        objects[key] = document
    return objects


def leaves(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten to leaf paths; lists of named items (env, containers by index) keep stable names."""

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(leaves(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list):
        out = {}
        for index, item in enumerate(value):
            label = item["name"] if isinstance(item, dict) and isinstance(item.get("name"), str) and prefix.endswith("env") else index
            out.update(leaves(item, f"{prefix}[{label}]"))
        return out
    return {prefix: value}


def changed_paths(before: Mapping, after: Mapping) -> set[str]:
    a, b = leaves(before), leaves(after)
    return {path for path in a.keys() | b.keys() if a.get(path, object()) != b.get(path, object())}


def identity(live: Mapping[Key, dict], rendered: Mapping[Key, dict]) -> list[str]:
    problems = [f"only live: {key}" for key in sorted(set(live) - set(rendered))]
    problems += [f"only rendered: {key}" for key in sorted(set(rendered) - set(live))]
    problems += [f"differs: {key} {sorted(changed_paths(live[key], rendered[key]))[:5]}"
                 for key in sorted(set(live) & set(rendered)) if live[key] != rendered[key]]
    return problems


def candidate(target: str, base: Mapping[Key, dict], cand: Mapping[Key, dict]) -> list[str]:
    expected = EXPECTED[target]
    problems = []
    added, removed = set(cand) - set(base), set(base) - set(cand)
    if added != expected["added"]:
        problems.append(f"added {sorted(added)} != expected {sorted(expected['added'])}")
    if removed:
        problems.append(f"removed {sorted(removed)}")
    changed = {key for key in set(base) & set(cand) if base[key] != cand[key]}
    if changed != set(expected["changed"]):
        problems.append(f"changed {sorted(changed)} != expected {sorted(expected['changed'])}")
    for key in changed & set(expected["changed"]):
        paths = changed_paths(base[key], cand[key])
        if paths != expected["changed"][key]:
            problems.append(f"{key} changed paths {sorted(paths)} != {sorted(expected['changed'][key])}")
    for key in added:
        if key[1] not in expected["namespaces"]:
            problems.append(f"added object outside {sorted(expected['namespaces'])}: {key}")
    values = _candidate_values(target, cand)
    for label, want in expected["values"].items():
        for got in values.get(label, [None]):
            if got != want:
                problems.append(f"{label} = {got!r}, expected {want!r}")
    return problems


def _candidate_values(target: str, cand: Mapping[Key, dict]) -> dict[str, list[Any]]:
    if target in ("kagent", "kagent-staging"):
        config = cand.get(("ConfigMap", "kagent", "kagent-controller"), {})
        return {"ConfigMap/kagent-controller WATCH_NAMESPACES": [(config.get("data") or {}).get("WATCH_NAMESPACES")]}
    found = []
    for key in EXPECTED["keda"]["changed"]:
        env = leaves(cand.get(key, {}))
        found.append(env.get("spec.template.spec.containers[0].env[WATCH_NAMESPACE].value"))
    return {"WATCH_NAMESPACE": found}


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    if len(args) == 3 and args[0] == "identity":
        problems = identity(load(open(args[1]).read()), load(open(args[2]).read()))
        label = "IDENTITY"
    elif len(args) == 4 and args[0] == "candidate" and args[1] in EXPECTED:
        base, cand = load(open(args[2]).read()), load(open(args[3]).read())
        problems = candidate(args[1], base, cand)
        label = "CANDIDATE"
        if not problems:
            print(json.dumps({"added": sorted(map(list, set(cand) - set(base))),
                              "changed": sorted(map(list, (k for k in set(base) & set(cand) if base[k] != cand[k])))}, indent=1))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(f"{label} FAIL: {problem}", file=sys.stderr)
    print(f"{label} {'PASS' if not problems else 'FAIL'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
