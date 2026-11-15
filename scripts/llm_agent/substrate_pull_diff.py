"""Exact diff for PKG-R (Substrate release): only atelet's --gcp-auth-for-image-pulls may change.

Kept outside ops/llm_agent (an image input) so the recorded images stay valid. It reuses the
shared helpers of ops/llm_agent/sandbox_watch_diff.py. Problems name objects and field paths
only, never values: these renders hold the live TLS key and session signing pools.

    python scripts/llm_agent/substrate_pull_diff.py identity <live-manifest> <baseline-render>
    python scripts/llm_agent/substrate_pull_diff.py candidate <baseline-render> <candidate-render>
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.llm_agent.sandbox_watch_diff import Key, changed_paths, identity, load  # noqa: E402

ATELET: Key = ("DaemonSet", "ate-system", "atelet")
CHANGED_PATHS = {"spec.template.spec.containers[0].args[0]"}
WANT = "--gcp-auth-for-image-pulls=true"


def candidate(base: Mapping[Key, dict], cand: Mapping[Key, dict]) -> list[str]:
    problems = []
    if set(cand) != set(base):
        problems.append(f"objects added {sorted(set(cand) - set(base))} removed {sorted(set(base) - set(cand))}")
    changed = sorted(key for key in set(base) & set(cand) if base[key] != cand[key])
    if changed != [ATELET]:
        problems.append(f"changed {changed} != expected {[ATELET]}")
    elif changed_paths(base[ATELET], cand[ATELET]) != CHANGED_PATHS:
        problems.append(f"{ATELET} changed paths {sorted(changed_paths(base[ATELET], cand[ATELET]))} != {sorted(CHANGED_PATHS)}")
    args = ((cand.get(ATELET, {}).get("spec") or {}).get("template", {}).get("spec", {}).get("containers") or [{}])[0].get("args") or [None]
    if args[0] != WANT:
        problems.append(f"atelet args[0] is not {WANT}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "identity":
        problems, label = identity(load(open(argv[1]).read()), load(open(argv[2]).read())), "IDENTITY"
    elif len(argv) == 3 and argv[0] == "candidate":
        base, cand = load(open(argv[1]).read()), load(open(argv[2]).read())
        problems, label = candidate(base, cand), "CANDIDATE"
        if not problems:
            print(json.dumps({"added": [], "changed": [list(ATELET)], "paths": sorted(CHANGED_PATHS)}))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(f"{label} FAIL: {problem}", file=sys.stderr)
    print(f"{label} {'PASS' if not problems else 'FAIL'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
