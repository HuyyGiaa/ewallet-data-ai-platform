"""Run the real Vault loader inside a pod and print one JSON result line.

Mounted next to ``ai_platform/inference/vault.py`` during validation. It prints the
outcome and a SHA-256 of the value, so a run can prove which version was read without
ever exposing it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, "/probe")
from vault import VaultError, load_from_environment  # noqa: E402


def main() -> int:
    """Attempt one load and report the result; denial is an expected outcome."""
    environment = dict(os.environ)
    try:
        loaded = load_from_environment(environment)
    except VaultError as error:
        print(json.dumps({"result": "denied", "code": error.code, "status": error.status}))
        return 0
    value = environment[loaded[0].env_name]
    print(json.dumps({
        "result": "loaded",
        "version": loaded[0].version,
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
