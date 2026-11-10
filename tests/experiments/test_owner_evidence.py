"""The owner's display helpers only read, and never put an API key on screen or in argv."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "scripts/experiments/owner_evidence.sh"
SECRET = "SECRET-VALUE-123"


def fakes(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    encoded = base64.b64encode(SECRET.encode()).decode()
    (bin_dir / "kubectl").write_text(f"""#!/bin/sh
echo "kubectl $*" >> {log}
case "$*" in *"get secret"*) printf '{encoded}';; *) echo ok;; esac
""")
    (bin_dir / "curl").write_text(f"""#!/bin/sh
echo "curl $*" >> {log}
for a in "$@"; do case "$a" in @*) echo "header-file $(cat "${{a#@}}" | wc -c) bytes" >> {log};; esac; done
echo 'HTTP/1.1 200 OK'
""")
    (bin_dir / "gcloud").write_text(f"#!/bin/sh\necho \"gcloud $*\" >> {log}\necho cpu-pool\n")
    for item in bin_dir.iterdir():
        item.chmod(0o755)
    return bin_dir


def run(tmp_path: Path, script: str) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = fakes(tmp_path)
    session = tmp_path / "session"
    (session / "safety").mkdir(parents=True)
    (session / "session.log").write_text("x session end status=0\n")
    (session / "safety/cleanup.txt").write_text("pool and namespace confirmed absent\n")
    (session / "production-before.txt").write_text("same\n")
    (session / "production-after.txt").write_text("same\n")
    result = subprocess.run(["bash", "-c", f"source {HELPERS}; {script}"], capture_output=True, text=True,
                            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "SESSION": str(session)},
                            timeout=60, check=False)
    return result, (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""


def test_every_display_helper_only_reads(tmp_path) -> None:
    result, calls = run(tmp_path, "ev_config; ev_route; ev_pods; ev_replicas; ev_provenance; ev_cleanup $SESSION")
    assert result.returncode == 0, result.stderr
    for verb in (" apply", " delete", " create", " patch", " scale", " edit", " annotate", " label", " rollout"):
        assert verb not in calls
    assert "production unchanged" in result.stdout


def test_policy_helpers_never_expose_the_key(tmp_path) -> None:
    script = ("ev_policy_valid; ev_policy_missing; ev_policy_wrong; ev_policy_backend_key; "
              "ev_policy_burst 13; ev_policy_route")
    result, calls = run(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert SECRET not in calls                     # not in any curl argv
    assert "header-file" in calls                   # the key travelled in a header file
    leftovers = [line for line in calls.splitlines() if line.startswith("curl") and "@/" in line]
    assert all(not Path(line.split("@", 1)[1].split()[0]).exists() for line in leftovers)
