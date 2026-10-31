"""The vllm-cpu-model Trivy secret config excludes one docstring and nothing else.

Offline checks read the config. The Trivy checks (EWALLET_RUN_TRIVY=1, Docker)
run the pinned Trivy secret scanner on a fixture tree laid out like the image:
the docstring alone is excluded, while synthetic credentials (generated here,
never committed) stay detected, including in the very files that carry the
docstring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "infra/docker/vllm-cpu-model/trivy-secret.yaml"
SCAN = ROOT / "scripts/llm_agent/scan_and_push.sh"
TRIVY = "docker.io/aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa"
SDK = "opt/venv/lib/python3.12/site-packages/anthropic"
# The three phrasings found verbatim in the anthropic SDK of the image.
DOCSTRING = ('        """\n        execution_identity: Execution principal for runs of this agent. Omitting it, sending null, or\n'
             '            sending `{"type": "service_account"}` stores the default.\n        """\n')
DOCSTRING_UPDATE = ('        """\n        execution_identity: Execution principal for runs of this agent. Full replacement. Omit to preserve;\n'
                    '              send null or `{"type": "service_account"}` to restore the default.\n        """\n')
DOCSTRING_PRESENT = ('    """\n    Always present; `{"type": "service_account"}` when runs carry no customer-named\n'
                     '    identity.\n    """\n')
# Synthetic, structurally valid-looking values; none is a real credential.
FAKE_KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + "Q" * 64
SERVICE_ACCOUNT = json.dumps({
    "type": "service_account", "project_id": "ewallet-synthetic", "private_key_id": "0" * 40,
    "private_key": f"-----BEGIN PRIVATE KEY-----\n{FAKE_KEY_BODY}\n-----END PRIVATE KEY-----\n",
    "client_email": "synthetic@ewallet-synthetic.iam.gserviceaccount.com"}, indent=2)
AWS_KEY_ID = "AKIA" + "Z7Q3XK2M9P4R8T6W"
GITHUB_PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def config():
    return yaml.safe_load(CONFIG.read_text())


class ConfigShapeTests(unittest.TestCase):
    def test_only_one_exclude_block_and_nothing_disabled_or_allowed(self):
        data = config()
        self.assertEqual(set(data), {"exclude-block"})
        for key in ("allow-rules", "disable-rules", "disable-allow-rules", "skip-patterns", "rules",
                    "enable-builtin-rules"):
            self.assertNotIn(key, data)
        self.assertEqual(len(data["exclude-block"]["regexes"]), 3)

    def test_each_regex_is_a_literal_docstring_phrase(self):
        patterns = [re.compile(p) for p in config()["exclude-block"]["regexes"]]
        for docstring in (DOCSTRING, DOCSTRING_UPDATE, DOCSTRING_PRESENT):
            self.assertEqual(sum(bool(p.search(docstring)) for p in patterns), 1, docstring)
        for pattern in patterns:
            for text in (SERVICE_ACCOUNT, '{"type": "service_account"}', '`{"type": "service_account"}`',
                         'sending `{"type": "service_account", "project_id": "x"}` stores the',
                         'send null or `{"type": "service_account", "private_key": "x"}`'):
                self.assertFalse(pattern.search(text), (pattern.pattern, text))
            # No wildcard, class or quantifier could stretch a block over other content.
            self.assertNotRegex(pattern.pattern, r"(?<!\\)[.*+?\[]")

    def test_gate_keeps_both_scanners_and_failing_exit_code(self):
        text = SCAN.read_text()
        self.assertIn("--scanners vuln,secret --exit-code 1 --severity CRITICAL,HIGH", text)
        self.assertIn("infra/docker/$image/trivy-secret.yaml", text)
        self.assertFalse(list((ROOT / "infra").glob("**/.trivyignore*")))


@unittest.skipUnless(os.environ.get("EWALLET_RUN_TRIVY") == "1" and shutil.which("docker"),
                     "set EWALLET_RUN_TRIVY=1 (needs Docker and the pinned Trivy image)")
class TrivySecretScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tree = Path(cls.tmp.name) / "tree"
        files = {
            # The exact SDK files: docstring only.
            f"{SDK}/resources/beta/agents/agents.py": DOCSTRING,
            # Same SDK file, docstring followed by a pasted key: the key must still be found.
            f"{SDK}/types/beta/agent_create_params.py": DOCSTRING + f"\nLEAKED = '''{SERVICE_ACCOUNT}'''\n",
            # The bare object without the sentence is not excluded.
            "srv/config/sa-type-only.json": '{"type": "service_account"}\n',
            # Other rules are untouched.
            "srv/config/sa.json": SERVICE_ACCOUNT,
            "srv/config/aws.env": f"AWS_ACCESS_KEY_ID={AWS_KEY_ID}\n",
            f"{SDK}/types/beta/agent_update_params.py": DOCSTRING_UPDATE + f"\ntoken = \"{GITHUB_PAT}\"\n",
            f"{SDK}/types/beta/beta_managed_agents_agent.py": DOCSTRING_PRESENT,
            # Same phrase but a pasted key right after it on the next lines.
            f"{SDK}/types/beta/beta_managed_agents_session_agent.py": DOCSTRING_PRESENT + SERVICE_ACCOUNT,
        }
        for name, body in files.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        shutil.copy(CONFIG, Path(cls.tmp.name) / "trivy-secret.yaml")
        cls.with_config = cls.scan(with_config=True)
        cls.without_config = cls.scan(with_config=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def scan(cls, *, with_config):
        args = ["docker", "run", "--rm", "-v", f"{cls.tmp.name}:/work:ro", TRIVY, "fs", "--scanners", "secret",
                "--severity", "CRITICAL,HIGH", "--format", "json", "--quiet", "--exit-code", "0"]
        if with_config:
            args += ["--secret-config", "/work/trivy-secret.yaml"]
        out = subprocess.run(args + ["/work/tree"], capture_output=True, text=True, check=True, timeout=600).stdout
        found = set()
        for result in json.loads(out).get("Results") or []:
            for secret in result.get("Secrets") or []:
                found.add((result["Target"], secret["RuleID"]))
        return found

    def test_docstring_alone_is_excluded_only_with_the_config(self):
        target = f"{SDK}/resources/beta/agents/agents.py"
        self.assertIn((target, "gcp-service-account"), self.without_config)
        self.assertNotIn((target, "gcp-service-account"), self.with_config)

    def test_real_shaped_key_in_an_affected_file_is_still_found(self):
        target = f"{SDK}/types/beta/agent_create_params.py"
        self.assertIn((target, "gcp-service-account"), self.with_config)
        self.assertIn((target, "private-key"), self.with_config)

    def test_bare_object_and_other_rules_still_fire(self):
        for expected in (("srv/config/sa-type-only.json", "gcp-service-account"),
                         ("srv/config/sa.json", "gcp-service-account"), ("srv/config/sa.json", "private-key"),
                         ("srv/config/aws.env", "aws-access-key-id"),
                         (f"{SDK}/types/beta/agent_update_params.py", "github-pat")):
            self.assertIn(expected, self.with_config)

    def test_config_removes_exactly_the_docstring_findings(self):
        removed = self.without_config - self.with_config
        self.assertEqual(removed, {(f"{SDK}/resources/beta/agents/agents.py", "gcp-service-account"),
                                   (f"{SDK}/types/beta/agent_update_params.py", "gcp-service-account"),
                                   (f"{SDK}/types/beta/beta_managed_agents_agent.py", "gcp-service-account")})
        for kept in ((f"{SDK}/types/beta/beta_managed_agents_session_agent.py", "gcp-service-account"),
                     (f"{SDK}/types/beta/beta_managed_agents_session_agent.py", "private-key")):
            self.assertIn(kept, self.with_config)
