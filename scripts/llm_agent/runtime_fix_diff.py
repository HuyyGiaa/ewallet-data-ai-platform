"""Exact diffs for PKG-T (runtime-stability images): one container image (+ one env var) per release.

    python scripts/llm_agent/runtime_fix_diff.py identity <live-manifest> <baseline-render>
    python scripts/llm_agent/runtime_fix_diff.py candidate kagent <baseline> <candidate> <image> <call-timeout>
    python scripts/llm_agent/runtime_fix_diff.py candidate substrate <baseline> <candidate> <image>

kagent: only Deployment kagent/kagent-controller may change, and in it only the controller
container's image (to <image>) and one added env var SUBSTRATE_CALL_TIMEOUT=<call-timeout>.
substrate: only Deployment ate-system/ate-api-server-deployment, only the ate-api-server image.
Kept outside ops/llm_agent (an image input). Problems name objects and paths, never values.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.llm_agent.sandbox_watch_diff import Key, changed_paths, identity, load  # noqa: E402

TARGETS: dict[str, tuple[Key, str]] = {
    "kagent": (("Deployment", "kagent", "kagent-controller"), "controller"),
    "substrate": (("Deployment", "ate-system", "ate-api-server-deployment"), "ate-api-server"),
}


def container(obj: Mapping, name: str) -> tuple[int, dict]:
    containers = obj["spec"]["template"]["spec"]["containers"]
    for index, item in enumerate(containers):
        if item.get("name") == name:
            return index, item
    raise KeyError(name)


def candidate(target: str, base: Mapping[Key, dict], cand: Mapping[Key, dict], image: str, timeout: str | None) -> list[str]:
    key, name = TARGETS[target]
    problems = []
    if set(cand) != set(base):
        problems.append(f"objects added {sorted(set(cand) - set(base))} removed {sorted(set(base) - set(cand))}")
    changed = sorted(k for k in set(base) & set(cand) if base[k] != cand[k])
    if changed != [key]:
        return problems + [f"changed {changed} != expected {[key]}"]
    try:
        index, after = container(cand[key], name)
        _, before = container(base[key], name)
    except KeyError:
        return problems + [f"{key} has no container {name}"]
    prefix = f"spec.template.spec.containers[{index}]"
    want = {f"{prefix}.image"}
    if target == "kagent":
        want |= {f"{prefix}.env[SUBSTRATE_CALL_TIMEOUT].name", f"{prefix}.env[SUBSTRATE_CALL_TIMEOUT].value"}
    got = changed_paths(base[key], cand[key])
    if got != want:
        problems.append(f"{key} changed paths {sorted(got)} != {sorted(want)}")
    if after.get("image") != image:
        problems.append(f"{key}/{name} image is not the expected patched digest")
    if target == "kagent":
        env = {e.get("name"): e.get("value") for e in after.get("env", [])}
        if env.get("SUBSTRATE_CALL_TIMEOUT") != timeout:
            problems.append("SUBSTRATE_CALL_TIMEOUT is not the reviewed value")
        if [e for e in after.get("env", []) if e.get("name") != "SUBSTRATE_CALL_TIMEOUT"] != before.get("env", []):
            problems.append("other controller env entries changed")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "identity":
        problems, label = identity(load(open(argv[1]).read()), load(open(argv[2]).read())), "IDENTITY"
    elif len(argv) in (5, 6) and argv[0] == "candidate" and argv[1] in TARGETS:
        target = argv[1]
        if (target == "kagent") != (len(argv) == 6):
            print(__doc__, file=sys.stderr)
            return 2
        base, cand = load(open(argv[2]).read()), load(open(argv[3]).read())
        problems, label = candidate(target, base, cand, argv[4], argv[5] if target == "kagent" else None), "CANDIDATE"
        if not problems:
            print(json.dumps({"target": target, "changed": [list(TARGETS[target][0])]}))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(f"{label} FAIL: {problem}", file=sys.stderr)
    print(f"{label} {'PASS' if not problems else 'FAIL'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
