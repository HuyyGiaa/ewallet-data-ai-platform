"""scripts/vault/shadow_compare.sh: SHADOW_MATCH only when both values exist, are non-empty and equal.

Runs the real script against a fake kubectl on PATH. No cluster, no secret values printed.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/vault/shadow_compare.sh"
VALUE = "redis.feature-store.svc.cluster.local:6379,password=synthetic-value-1"
OTHER = "redis.feature-store.svc.cluster.local:6379,password=synthetic-value-2"
pytestmark = pytest.mark.skipif(not (shutil.which("jq") and shutil.which("base64")), reason="needs jq and base64")

FAKE_KUBECTL = """#!/usr/bin/env bash
# fake kubectl: secret <name> -> $FAKE_DIR/<name>.json; "unreachable" -> connection error; else NotFound
name=""; prev=""
for arg in "$@"; do [ "$prev" = secret ] && name=$arg; prev=$arg; done
[ "$name" = unreachable ] && { echo 'The connection to the server was refused' >&2; exit 1; }
[ -f "$FAKE_DIR/$name.json" ] && { cat "$FAKE_DIR/$name.json"; exit 0; }
echo "Error from server (NotFound): secrets \\"$name\\" not found" >&2; exit 1
"""


def secret(value=None, *, key="connection_string", raw=None):
    if raw is not None:
        return raw
    data = {} if value is None else {key: base64.b64encode(value.encode()).decode() if value else ""}
    return json.dumps({"kind": "Secret", "data": data})


def compare(tmp_path, existing, shadow, *, existing_name="feast-online-redis", shadow_name="feature-api-redis-vault"):
    bin_dir, fake_dir = tmp_path / "bin", tmp_path / "secrets"
    bin_dir.mkdir(exist_ok=True)
    fake_dir.mkdir(exist_ok=True)
    (bin_dir / "kubectl").write_text(FAKE_KUBECTL)
    (bin_dir / "kubectl").chmod(0o755)
    for name, body in ((existing_name, existing), (shadow_name, shadow)):
        if body is not None:
            (fake_dir / f"{name}.json").write_text(body)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DIR": str(fake_dir)}
    done = subprocess.run(["bash", str(SCRIPT), "kind-test", "ml-platform", existing_name, shadow_name],
                          capture_output=True, text=True, env=env, timeout=30)
    output = done.stdout + done.stderr
    for plaintext in (VALUE, OTHER, "synthetic-value"):
        assert plaintext not in output
    return done.returncode, done.stdout.strip()


def test_equal_values_match(tmp_path):
    assert compare(tmp_path, secret(VALUE), secret(VALUE)) == (0, "SHADOW_MATCH")


@pytest.mark.parametrize("existing,shadow,status", [
    (secret(VALUE), secret(OTHER), "SHADOW_MISMATCH"),
    (None, secret(VALUE), "SHADOW_EXISTING_SECRET_MISSING"),
    (secret(VALUE), None, "SHADOW_SHADOW_SECRET_MISSING"),
    (None, None, "SHADOW_EXISTING_SECRET_MISSING"),
    (secret(VALUE, key="password"), secret(VALUE), "SHADOW_EXISTING_KEY_MISSING"),
    (secret(VALUE), secret(VALUE, key="other"), "SHADOW_SHADOW_KEY_MISSING"),
    (secret(VALUE), secret(None), "SHADOW_SHADOW_KEY_MISSING"),                     # Secret without data
    (secret(""), secret(""), "SHADOW_EXISTING_VALUE_EMPTY"),                        # equal, but empty
    (secret(VALUE), secret(""), "SHADOW_SHADOW_VALUE_EMPTY"),
    (secret(VALUE), secret(raw='{"data": {"connection_string": "!!not-base64!!"}}'), "SHADOW_SHADOW_READ_FAILED"),
    (secret(VALUE), secret(raw='{"data": {"connection_string": null}}'), "SHADOW_SHADOW_READ_FAILED"),
    (secret(raw="not json"), secret(VALUE), "SHADOW_EXISTING_READ_FAILED"),
], ids=["differ", "existing-missing", "shadow-missing", "both-missing", "existing-key-missing", "shadow-key-missing",
        "shadow-no-data", "both-empty", "shadow-empty", "invalid-base64", "null-value", "invalid-json"])
def test_every_other_outcome_has_its_own_failure_status(tmp_path, existing, shadow, status):
    assert compare(tmp_path, existing, shadow) == (1, status)


def test_kubectl_failure_is_not_reported_as_a_missing_secret(tmp_path):
    assert compare(tmp_path, None, secret(VALUE), existing_name="unreachable") == (1, "SHADOW_EXISTING_READ_FAILED")
