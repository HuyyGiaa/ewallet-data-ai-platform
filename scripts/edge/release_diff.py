"""Gate for a Helm upgrade of a frozen release (G3-B: grafanaExternal.rootUrl on llm-agent).

Compares rendered manifests object by object and fails unless the change is exactly what was
approved. Used twice, with files produced from the live release:

  helm get manifest llm-agent -n default            > live.yaml
  helm get values llm-agent -n default -a -o yaml   > values.yaml   (private dir: may hold settings)
  helm template llm-agent infra/helm/llm-agent-platform -n default -f values.yaml > current.yaml
  helm template ... -f values.yaml --set grafanaExternal.rootUrl=https://observe.<D>/ > proposed.yaml

  release_diff.py --before live.yaml --after current.yaml
      no drift: the current chart with the live values reproduces what is deployed
  release_diff.py --before current.yaml --after proposed.yaml --allow-grafana-root-url https://observe.<D>/
      the only change is GF_SERVER_ROOT_URL on Deployment ewallet-agents/grafana

Prints the changed objects (kind/namespace/name) and never their contents.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import yaml


def objects(path: Path) -> dict[tuple, dict]:
    return {(d["kind"], (d.get("metadata") or {}).get("namespace", ""), d["metadata"]["name"]): d
            for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d and "kind" in d}


def without_root_url(deployment: dict) -> dict:
    d = copy.deepcopy(deployment)
    for c in d["spec"]["template"]["spec"]["containers"]:
        c["env"] = [e for e in c.get("env", []) if e.get("name") != "GF_SERVER_ROOT_URL"]
    return d


def diff(before: dict[tuple, dict], after: dict[tuple, dict], root_url: str | None) -> tuple[list[str], list[str]]:
    """(problems, allowed changes)."""
    problems, allowed = [], []
    for key in sorted(set(before) | set(after), key=str):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        where = "/".join(key)
        if root_url and key[0] == "Deployment" and key[2] == "grafana" and a and b and without_root_url(a) == without_root_url(b):
            envs = [e for c in b["spec"]["template"]["spec"]["containers"] for e in c.get("env", []) if e.get("name") == "GF_SERVER_ROOT_URL"]
            if envs == [{"name": "GF_SERVER_ROOT_URL", "value": root_url}]:
                allowed.append(f"{where}: GF_SERVER_ROOT_URL={root_url}")
                continue
        problems.append(f"{where}: {'added' if a is None else 'removed' if b is None else 'changed'}")
    return problems, allowed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--allow-grafana-root-url", help="the one approved change")
    args = parser.parse_args(argv)
    problems, allowed = diff(objects(args.before), objects(args.after), args.allow_grafana_root_url)
    print(json.dumps({"allowed_changes": allowed, "unexpected_changes": problems}, indent=2))
    print("RELEASE_DIFF_OK" if not problems else "RELEASE_DIFF_UNEXPECTED")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
