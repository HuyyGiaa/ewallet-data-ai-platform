"""Render the Fraud API Service and Deployment for one replica owner.

Two modes, because exactly one writer may own ``spec.replicas``:

- ``fixed``: the manifest carries an explicit replica count. Used for the fixed
  baseline arms and for every deployment while KEDA is not installed.
- ``keda``: the manifest has no ``spec.replicas``. The KEDA ScaledObject in
  ``infra/helm/ewallet-serving`` owns the count through its HPA.

The template stays usable with plain ``envsubst`` for the fixed mode, so existing
runbooks keep working. Switching a live Deployment from ``fixed`` to ``keda`` is
not a plain apply: ``scripts/serving/fraud_api_keda_handover.sh`` moves ownership
without letting client-side apply reset the Deployment to one replica.

Usage::

    FRAUD_API_IMAGE=... ONLINE_REGISTRY_URI=... MODEL_VERSION=... GIT_SHA=... \\
        python -m ops.serving.fraud_api_manifest --mode fixed --replicas 2
    ... python -m ops.serving.fraud_api_manifest --mode keda
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra/kubernetes/fraud-serving/fraud-api.yaml.tmpl"
DEPLOYMENT_NAME = "fraud-api"
NAMESPACE = "ml-platform"
REPLICAS_LINE = "  replicas: ${FRAUD_API_REPLICAS}\n"
REQUIRED = ("FRAUD_API_IMAGE", "ONLINE_REGISTRY_URI", "MODEL_VERSION", "GIT_SHA")
MAX_REPLICAS = 10
_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
_PINNED_IMAGE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")


class RenderError(ValueError):
    """The requested manifest cannot be rendered safely."""


def render(
    mode: str,
    values: Mapping[str, str],
    *,
    replicas: int | None = None,
    template: Path = TEMPLATE,
) -> str:
    """Return the manifest for ``mode``; raise RenderError on any unsafe input.

    Args:
        mode: ``fixed`` or ``keda``.
        values: Template variables; only the names the template uses are read.
        replicas: Required for ``fixed`` (1 to MAX_REPLICAS); refused for ``keda``.
        template: The template file, the repository template by default.
    """

    text = template.read_text(encoding="utf-8")
    if text.count(REPLICAS_LINE) != 1:
        raise RenderError("the template must hold exactly one replicas line")
    if mode == "fixed":
        if replicas is None or not 1 <= replicas <= MAX_REPLICAS:
            raise RenderError(f"fixed mode needs --replicas between 1 and {MAX_REPLICAS}")
        text = text.replace("${FRAUD_API_REPLICAS}", str(replicas))
    elif mode == "keda":
        if replicas is not None:
            raise RenderError("keda mode owns no replica count; drop --replicas")
        text = text.replace(REPLICAS_LINE, "")
    else:
        raise RenderError(f"unknown mode {mode!r}; use fixed or keda")

    missing = [name for name in REQUIRED if not values.get(name, "").strip()]
    if missing:
        raise RenderError(f"missing template values: {', '.join(missing)}")
    if not _PINNED_IMAGE.match(values["FRAUD_API_IMAGE"]):
        raise RenderError("FRAUD_API_IMAGE must be pinned by digest")
    used = set(_PLACEHOLDER.findall(text))
    for name in sorted(used & set(values)):
        if any(char in values[name] for char in '"\n\\'):
            raise RenderError(f"{name} holds a character that would break the manifest")

    text = _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), text)
    leftover = sorted(set(_PLACEHOLDER.findall(text)))
    if leftover:
        raise RenderError(f"unresolved template values: {', '.join(leftover)}")
    return text


def main(argv: list[str] | None = None) -> int:
    """Print the rendered manifest; values come from the environment."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", required=True, choices=("fixed", "keda"))
    parser.add_argument("--replicas", type=int)
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.mode, os.environ, replicas=args.replicas))
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
