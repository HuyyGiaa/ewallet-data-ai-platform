"""Exact diffs for PKG-T (runtime-stability images): one container image (+ one env var) per release.

    python scripts/llm_agent/runtime_fix_diff.py identity <live-manifest> <baseline-render>
    python scripts/llm_agent/runtime_fix_diff.py candidate kagent <baseline> <candidate> <image> <call-timeout>
    python scripts/llm_agent/runtime_fix_diff.py candidate substrate <baseline> <candidate> <image> [<req>:<limit> [<controller-image>]]
    python scripts/llm_agent/runtime_fix_diff.py candidate kagent-otel <baseline> <candidate> <endpoint>
    python scripts/llm_agent/runtime_fix_diff.py candidate atelet-placement <baseline> <candidate>

kagent: only Deployment kagent/kagent-controller may change, and in it only the controller
container's image (to <image>) and one added env var SUBSTRATE_CALL_TIMEOUT=<call-timeout>.
substrate: only Deployment ate-system/ate-api-server-deployment (the ate-api-server image) and,
with <atelet-memory>, DaemonSet ate-system/atelet (only the atelet memory request and limit).
kagent-otel (P2-C): only ConfigMap kagent/kagent-controller key OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (to
<endpoint>) and the controller Deployment's checksum/configmap annotation (the rollout trigger).
atelet-placement (P2-B): only the added PriorityClass ewallet-substrate-node-agent (value 100000,
preemptionPolicy Never, not the global default) and, in DaemonSet ate-system/atelet, its
priorityClassName and the added not-ready NoSchedule toleration.
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


ATELET: Key = ("DaemonSet", "ate-system", "atelet")
ATECONTROLLER: Key = ("Deployment", "ate-system", "ate-controller")


def atelet_problems(base: Mapping, cand: Mapping, memory: str) -> list[str]:
    index, after = container(cand, "atelet")
    prefix = f"spec.template.spec.containers[{index}].resources"
    want = {f"{prefix}.requests.memory", f"{prefix}.limits.memory"}
    got = changed_paths(base, cand)
    problems = [] if got == want else [f"{ATELET} changed paths {sorted(got)} != {sorted(want)}"]
    request, _, limit = memory.partition(":")
    if (after["resources"]["requests"]["memory"], after["resources"]["limits"]["memory"]) != (request, limit):
        problems.append("atelet memory is not the reviewed value")
    return problems


def controller_problems(base: Mapping, cand: Mapping, image: str) -> list[str]:
    """B′: ate-controller may change only its container image, to the patched digest."""
    index, after = container(cand, "ate-controller")
    problems = []
    got = changed_paths(base, cand)
    if got - {f"spec.template.spec.containers[{index}].image"}:
        problems.append(f"{ATECONTROLLER} changed paths {sorted(got)} beyond the image")
    if after.get("image") != image:
        problems.append(f"{ATECONTROLLER} image is not the expected patched digest")
    return problems


def candidate(target: str, base: Mapping[Key, dict], cand: Mapping[Key, dict], image: str, timeout: str | None,
              atelet_memory: str | None = None, controller_image: str | None = None) -> list[str]:
    """The candidate holds the reviewed end state; only the reviewed objects and paths differ.

    For substrate a step may find part of PKG-T already live (baseline rendered with it), so the
    changed set is a non-empty subset of the reviewed objects and the end state is checked in full.
    """
    key, name = TARGETS[target]
    allowed = {key} | ({ATELET} if target == "substrate" and atelet_memory else set()) \
        | ({ATECONTROLLER} if target == "substrate" and controller_image else set())
    problems = []
    if set(cand) != set(base):
        problems.append(f"objects added {sorted(set(cand) - set(base))} removed {sorted(set(base) - set(cand))}")
    changed = {k for k in set(base) & set(cand) if base[k] != cand[k]}
    if not changed or not changed <= allowed or (target == "kagent" and changed != {key}):
        return problems + [f"changed {sorted(changed)} not within {sorted(allowed)}"]
    if ATELET in allowed:
        if ATELET in changed:
            problems += atelet_problems(base[ATELET], cand[ATELET], atelet_memory)
        else:
            _, after = container(cand[ATELET], "atelet")
            request, _, limit = atelet_memory.partition(":")
            if (after["resources"]["requests"]["memory"], after["resources"]["limits"]["memory"]) != (request, limit):
                problems.append("atelet memory is not the reviewed value")
    if ATECONTROLLER in allowed:
        problems += controller_problems(base[ATECONTROLLER], cand[ATECONTROLLER], controller_image)
    try:
        index, after = container(cand[key], name)
        _, before = container(base[key], name)
    except KeyError:
        return problems + [f"{key} has no container {name}"]
    if after.get("image") != image:
        problems.append(f"{key}/{name} image is not the expected patched digest")
    if key not in changed:
        return problems
    prefix = f"spec.template.spec.containers[{index}]"
    want = {f"{prefix}.image"}
    if target == "kagent" and "SUBSTRATE_CALL_TIMEOUT" not in {e.get("name") for e in before.get("env", [])}:
        want |= {f"{prefix}.env[SUBSTRATE_CALL_TIMEOUT].name", f"{prefix}.env[SUBSTRATE_CALL_TIMEOUT].value"}
    got = changed_paths(base[key], cand[key])
    if got != want:
        problems.append(f"{key} changed paths {sorted(got)} != {sorted(want)}")
    if target == "kagent":
        env = {e.get("name"): e.get("value") for e in after.get("env", [])}
        if env.get("SUBSTRATE_CALL_TIMEOUT") != timeout:
            problems.append("SUBSTRATE_CALL_TIMEOUT is not the reviewed value")
        if [e for e in after.get("env", []) if e.get("name") != "SUBSTRATE_CALL_TIMEOUT"] != \
                [e for e in before.get("env", []) if e.get("name") != "SUBSTRATE_CALL_TIMEOUT"]:
            problems.append("other controller env entries changed")
    return problems


KAGENT_CONFIGMAP: Key = ("ConfigMap", "kagent", "kagent-controller")
KAGENT_DEPLOYMENT: Key = ("Deployment", "kagent", "kagent-controller")
OTEL_KEY = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"


def kagent_otel(base: Mapping[Key, dict], cand: Mapping[Key, dict], endpoint: str) -> list[str]:
    """P2-C: the controller's OTLP traces endpoint, nothing else (its image and env stay as they are)."""
    problems = []
    if not endpoint.startswith(("http://", "https://")):
        problems.append("the endpoint has no URL scheme (OTEL Go would parse an empty host)")
    if set(cand) != set(base):
        problems.append(f"objects added {sorted(set(cand) - set(base))} removed {sorted(set(base) - set(cand))}")
    changed = {k for k in set(base) & set(cand) if base[k] != cand[k]}
    want = {KAGENT_CONFIGMAP, KAGENT_DEPLOYMENT}
    if changed != want:
        return problems + [f"changed {sorted(changed)} != {sorted(want)}"]
    got = changed_paths(base[KAGENT_CONFIGMAP], cand[KAGENT_CONFIGMAP])
    if got != {f"data.{OTEL_KEY}"}:
        problems.append(f"{KAGENT_CONFIGMAP} changed paths {sorted(got)} != ['data.{OTEL_KEY}']")
    if cand[KAGENT_CONFIGMAP].get("data", {}).get(OTEL_KEY) != endpoint:
        problems.append(f"{OTEL_KEY} is not the reviewed endpoint")
    got = changed_paths(base[KAGENT_DEPLOYMENT], cand[KAGENT_DEPLOYMENT])
    if got != {"spec.template.metadata.annotations.checksum/configmap"}:
        problems.append(f"{KAGENT_DEPLOYMENT} changed paths {sorted(got)} != the checksum/configmap annotation")
    return problems


ATELET_CLASS = "ewallet-substrate-node-agent"
ATELET_CLASS_KEY: Key = ("PriorityClass", "", ATELET_CLASS)
NOT_READY = {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoSchedule"}


def atelet_placement(base: Mapping[Key, dict], cand: Mapping[Key, dict]) -> list[str]:
    """P2-B: atelet's non-preempting class and not-ready toleration; nothing else."""
    problems = []
    added, removed = set(cand) - set(base), set(base) - set(cand)
    if added != {ATELET_CLASS_KEY} or removed:
        return [f"objects added {sorted(added)} removed {sorted(removed)}; want only {ATELET_CLASS_KEY}"]
    pc = cand[ATELET_CLASS_KEY]
    if (pc.get("value"), pc.get("preemptionPolicy"), bool(pc.get("globalDefault"))) != (100000, "Never", False):
        problems.append("the PriorityClass must be value 100000, preemptionPolicy Never, not the global default")
    changed = {k for k in set(base) & set(cand) if base[k] != cand[k]}
    if changed != {ATELET}:
        return problems + [f"changed {sorted(changed)} != [{ATELET}]"]
    before, after = base[ATELET]["spec"]["template"]["spec"], cand[ATELET]["spec"]["template"]["spec"]
    if after.get("priorityClassName") != ATELET_CLASS:
        problems.append("atelet priorityClassName is not the reviewed class")
    old_tol, new_tol = list(before.get("tolerations") or []), list(after.get("tolerations") or [])
    if new_tol != old_tol + [NOT_READY]:
        problems.append("atelet tolerations must be the existing ones plus exactly the not-ready NoSchedule toleration")
    rest_before = {k: v for k, v in before.items() if k not in ("priorityClassName", "tolerations")}
    rest_after = {k: v for k, v in after.items() if k not in ("priorityClassName", "tolerations")}
    outside = {k: v for k, v in base[ATELET].items() if k != "spec"} != {k: v for k, v in cand[ATELET].items() if k != "spec"}
    spec_rest = ({k: v for k, v in base[ATELET]["spec"].items() if k != "template"}
                 != {k: v for k, v in cand[ATELET]["spec"].items() if k != "template"})
    tmpl_meta = base[ATELET]["spec"]["template"].get("metadata") != cand[ATELET]["spec"]["template"].get("metadata")
    if rest_before != rest_after or outside or spec_rest or tmpl_meta:
        problems.append("atelet changed outside priorityClassName and tolerations")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "identity":
        problems, label = identity(load(open(argv[1]).read()), load(open(argv[2]).read())), "IDENTITY"
    elif len(argv) == 4 and argv[0] == "candidate" and argv[1] == "atelet-placement":
        base, cand = load(open(argv[2]).read()), load(open(argv[3]).read())
        problems, label = atelet_placement(base, cand), "CANDIDATE"
        if not problems:
            print(json.dumps({"target": "atelet-placement", "added": [list(ATELET_CLASS_KEY)], "changed": [list(ATELET)]}))
    elif len(argv) == 5 and argv[0] == "candidate" and argv[1] == "kagent-otel":
        base, cand = load(open(argv[2]).read()), load(open(argv[3]).read())
        problems, label = kagent_otel(base, cand, argv[4]), "CANDIDATE"
        if not problems:
            print(json.dumps({"target": "kagent-otel", "changed": sorted(list(k) for k in set(base) & set(cand) if base[k] != cand[k])}))
    elif len(argv) in (5, 6, 7) and argv[0] == "candidate" and argv[1] in TARGETS:
        target = argv[1]
        if target == "kagent" and len(argv) != 6:
            print(__doc__, file=sys.stderr)
            return 2
        base, cand = load(open(argv[2]).read()), load(open(argv[3]).read())
        extra = argv[5] if len(argv) == 6 else None
        controller = argv[6] if len(argv) == 7 and target == "substrate" else None
        problems = (candidate(target, base, cand, argv[4], extra) if target == "kagent"
                    else candidate(target, base, cand, argv[4], None, extra, controller))
        label = "CANDIDATE"
        if not problems:
            print(json.dumps({"target": target, "changed": sorted(list(k) for k in set(base) & set(cand) if base[k] != cand[k])}))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(f"{label} FAIL: {problem}", file=sys.stderr)
    print(f"{label} {'PASS' if not problems else 'FAIL'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
