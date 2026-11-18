"""Agent integration: fail-closed gates of scripts/llm_agent/agi_campaign.sh.

Every external command (helm, kubectl, gcloud, the RAG runner and the Python
probe runtime) is a fake that records its arguments, so the tests prove which
commands run and which never run. No cluster, registry or bucket is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/llm_agent/agi_campaign.sh"
DIFF = ROOT / "scripts/llm_agent/helm_object_diff.py"
CAMPAIGN = "agi-20261115T000000Z"
CTX = "gke_test_ctx"
IMAGE = "asia-southeast1-docker.pkg.dev/p/r/{}@sha256:" + "b" * 64
MANIFEST = b'{"lineage": {"snapshot_id": "feast-r5gb-20261111-214544"}}'


def doc(kind: str, namespace: str, name: str, **spec) -> dict:
    return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": namespace}, "spec": spec}


MCP_LIVE = [doc("Deployment", "ewallet-mcp", "feature-rag-mcp", image="old"),
            doc("Deployment", "ewallet-mcp", "drift-mcp", image="old"),
            doc("Service", "ewallet-mcp", "drift-mcp", port=8080)]
MCP_ON = [doc("Deployment", "ewallet-mcp", "feature-rag-mcp", image="new"),
          doc("Deployment", "ewallet-mcp", "drift-mcp", image="new"),
          doc("Service", "ewallet-mcp", "drift-mcp", port=8080)]
AGENTS_LIVE = [doc("Agent", "ewallet-agents", name, version="1.0.0")
               for name in ("fraud-coordinator", "feature-rag-specialist", "drift-specialist")]
AGENTS_ON = [doc("Agent", "ewallet-agents", name, version="1.1.0")
             for name in ("fraud-coordinator", "feature-rag-specialist", "drift-specialist")] + [
    doc("Role", "ewallet-agents", "ewallet-global-llm-reader"),
    doc("RoleBinding", "ewallet-agents", "ewallet-global-llm-reader"),
    doc("NetworkPolicy", "ewallet-agents", "allow-byo-modelconfig-to-kube-apiserver")]

FAKE_HELM = r'''#!/usr/bin/env bash
echo "helm $*" >> "$FAKE_LOG"
args=(); while (($#)); do case "$1" in --kube-context|-n) shift 2 ;; *) args+=("$1"); shift ;; esac; done
set -- "${args[@]}"
case "$1" in
  status) echo '{"info": {"status": "deployed"}, "version": 7}' ;;
  get) if [[ "$2" == values ]]; then [[ -n "${FAKE_VALUES:-}" ]] && cat "$FAKE_VALUES" || echo "a: 1"
       else cat "$FAKE_DIR/$3-live.yaml"; fi ;;
  template)
    [[ -n "${FAKE_TEMPLATE_FAIL:-}" ]] && { echo "template error" >&2; exit 1; }
    if [[ "$*" == *agi/* || ( -n "${FAKE_ON_MATCH:-}" && "$*" == *"${FAKE_ON_MATCH}"* ) ]]; then
      cat "$FAKE_DIR/$2-on.yaml"; else cat "$FAKE_DIR/$2-live.yaml"; fi ;;
  upgrade)
    if [[ "$*" == *--dry-run=server* ]]; then exit "${FAKE_DRYRUN_RC:-0}"; fi
    echo "REAL-UPGRADE $2" >> "$FAKE_LOG" ;;
  history) echo "REVISION 8 deployed" ;;
  rollback) echo "REAL-ROLLBACK $2 $3" >> "$FAKE_LOG" ;;
  *) exit 3 ;;
esac
'''
FAKE_KUBECTL = r'''#!/usr/bin/env bash
echo "kubectl $*" >> "$FAKE_LOG"
case "$*" in
  "config current-context") echo "$FAKE_CTX" ;;
  *"get deploy -o json"*) echo '{"items": [{"metadata": {"name": "x"}, "spec": {"replicas": 1}, "status": {"availableReplicas": 1}}]}' ;;
  *"get agents.kagent.dev drift-specialist"*) [[ -n "${FAKE_DRIFT_ABSENT:-}" ]] && exit 1; printf %s "${FAKE_DRIFT_TYPE:-Declarative}" ;;
  *"get agents.kagent.dev fraud-coordinator"*) printf %s "${FAKE_COORDINATOR_TYPE:-BYO}" ;;
  *"-n ewallet-sandbox get sandboxagents.kagent.dev"*) [[ -n "${FAKE_SANDBOXAGENTS_MISSING:-}" ]] && exit 1
    printf 'sandboxagent.kagent.dev/feature-rag-specialist\nsandboxagent.kagent.dev/drift-specialist\n' ;;
  *"delete scaledobject"*) echo "scaledobject deleted" ;;
  *"get hpa"*) [[ -n "${FAKE_HPA_MISSING:-}" ]] && exit 1; printf 2 ;;
  *"get networkpolicy -o name"*) echo networkpolicy.networking.k8s.io/drift-specialist-to-mcp-gateway
    echo networkpolicy.networking.k8s.io/allow-sandbox-workers-to-mcp-gateway-ingress ;;
  *"get secret"*"-o name"*) echo secret/x ;;
  *"get secret"*token*) printf %s "$(printf 't%.0s' {1..40})" | base64 ;;
  *"get svc kubernetes"*) [[ -n "${FAKE_SVC_FAIL:-}" ]] && exit 1; echo 10.0.0.1 ;;
  *endpointslices*) echo 10.0.0.2 ;;
  *) echo "unexpected kubectl $*" >&2; exit 3 ;;
esac
'''
FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "gcloud $*" >> "$FAKE_LOG"
case "$2" in
  cat) [[ "$3" == *export_manifest.json ]] && cat "$FAKE_DIR/manifest.json" || echo registry ;;
  ls) echo "2275 registry.db" ;;
esac
'''
FAKE_RAG = r'''#!/usr/bin/env bash
echo "rag $*" >> "$FAKE_LOG"
case "$1" in
  dbs) printf ' db      keys  owner\n  0    173382  -\n  3         5  ewallet_rag_hybrid_20261114t144825 in use\n' ;;
  digest) echo "db=$2 keys=5 sha256=${FAKE_DB3_DIGEST:-aaaa}" ;;
esac
'''
FAKE_PY = r'''#!/usr/bin/env bash
# Stand-in for the uv runtime running scripts/llm_agent/agi_probe.py.
echo "probe $*" >> "$FAKE_LOG"
if [[ "$*" == *" mcp "* && "$*" != *"--tool feature_lookup"* ]]; then
  [[ -n "${EWALLET_MCP_SERVICE_TOKEN:-}" && -n "${AGI_CONTROL_TOKEN:-}" ]] || { echo "tokens missing from env" >&2; exit 9; }
fi
shift   # the agi_probe.py path
evidence=""; while (($#)); do [[ "$1" == --evidence || "$1" == --out ]] && evidence=$2; shift; done
[[ -n "$evidence" ]] && echo '{"structured_content": {"result": {"historical_features": {"f": 1}}}}' > "$evidence"
key=$(basename "${evidence:-none}")
rc=$(python3 -c 'import json,os,sys;print(json.loads(os.environ.get("FAKE_PROBE_RC","{}")).get(sys.argv[1],0))' "$key")
exit "$rc"
'''


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "bash and git required")
class CampaignGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agi-gates-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD),
                           ("rag", FAKE_RAG), ("probe-python", FAKE_PY)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        (self.fake / "manifest.json").write_bytes(MANIFEST)
        self.write("llm-agent-mcp", MCP_LIVE, MCP_ON)
        self.write("llm-agent", AGENTS_LIVE, AGENTS_ON)
        self.git = self.tmp / "checkout"
        self.git.mkdir()
        subprocess.run(["git", "init", "-q", str(self.git)], check=True)
        (self.git / "file").write_text("x")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": os.environ["PATH"]}
        subprocess.run(["git", "-C", str(self.git), "add", "file"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.git), "commit", "-qm", "x"], check=True, env=env)
        self.sha = subprocess.run(["git", "-C", str(self.git), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
        self.ev = self.tmp / "evidence"
        describe = self.tmp / "describe.json"
        describe.write_text(json.dumps({"environment": {
            "EWALLET_DRIFT_ALLOW_SYNTHETIC": "false", "EWALLET_DRIFT_BASELINES_SHA256": "d" * 64,
            "EWALLET_DRIFT_WINDOW_MANIFEST_SHA256": "e" * 64,
            "EWALLET_DRIFT_DATASET_REVISION": "feast-r5gb-20261111-214544"}}))
        self.campaign = {
            "CAMPAIGN": CAMPAIGN, "EV": str(self.ev), "CTX": CTX, "SHA": self.sha,
            "SNAPSHOT_MANIFEST_SHA256": hashlib.sha256(MANIFEST).hexdigest(),
            "MCP_IMAGE": IMAGE.format("mcp-tools"), "DRIFT_DESCRIBE": str(describe), "FRAUD_DB": "0", "RAG_DB": "3",
            "COORDINATOR_IMAGE": IMAGE.format("c"), "FEATURE_RAG_IMAGE": IMAGE.format("r"),
            "DRIFT_IMAGE": IMAGE.format("d"), "APISERVER_CIDRS": "10.0.0.1/32,10.0.0.2/32",
            "BASELINE": "bl-r5gb-202604.account_tx_count_1h",
        }
        self.log = self.tmp / "calls.log"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, release, live, on):
        (self.fake / f"{release}-live.yaml").write_text(yaml.safe_dump_all(live))
        (self.fake / f"{release}-on.yaml").write_text(yaml.safe_dump_all(on))

    def run_script(self, *args, campaign=None, **env):
        config = self.tmp / "campaign.env"
        config.write_text("".join(f"{key}={value}\n" for key, value in (campaign or self.campaign).items()))
        variables = {**os.environ, "AGI_ENV": str(config), "AGI_PRIVATE": str(self.tmp / "private"),
                     "AGI_GIT_DIR": str(self.git), "HELM": str(self.bin / "helm"), "KUBECTL": str(self.bin / "kubectl"),
                     "GCLOUD": str(self.bin / "gcloud"), "AGI_RAG_RUNNER": str(self.bin / "rag"),
                     "AGI_AGENT_PY": str(self.bin / "probe-python"), "PYTHON": sys.executable,
                     "FAKE_LOG": str(self.log), "FAKE_DIR": str(self.fake), "FAKE_CTX": CTX, **env}
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=variables, timeout=120)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def preflight(self, **env):
        result = self.run_script("preflight", **env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.preflight_calls = self.calls()
        self.log.unlink()   # later assertions see only the commands of the step under test
        return result

    # ---------------------------------------------------------------- Stage 0

    def test_passing_preflight_writes_the_marker_and_never_mutates(self):
        self.preflight()
        marker = (self.ev / "stage0/PREFLIGHT_PASS").read_text()
        self.assertIn(f"sha={self.sha}", marker)
        self.assertIn("LLM_AGENT_REVISION=7", marker)
        self.assertIn("LLM_AGENT_MCP_REVISION=7", marker)
        self.assertNotIn("REAL-", self.preflight_calls)
        self.assertNotIn("upgrade", self.preflight_calls)
        self.assertNotRegex(self.preflight_calls, r"kubectl .*(apply|create|delete|patch|scale|rollout restart)")

    def test_failed_preflight_check_blocks_the_marker_even_through_tee(self):
        (self.fake / "manifest.json").write_bytes(b"another snapshot")
        result = self.run_script("preflight")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.ev / "stage0/PREFLIGHT_PASS").exists())
        self.assertIn("FAIL P0.9 drift snapshot manifest digest", (self.ev / "campaign.log").read_text())

    def test_a_failing_command_inside_a_check_fails_it_even_when_the_last_command_succeeds(self):
        # apiserver_addresses: the Service query fails, the endpoint query succeeds and the file is non-empty.
        result = self.run_script("preflight", FAKE_SVC_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL P0.13 API server addresses", (self.ev / "campaign.log").read_text())

    def test_dirty_or_other_commit_fails_preflight(self):
        (self.git / "file").write_text("changed")
        self.assertNotEqual(self.run_script("preflight").returncode, 0)
        self.assertFalse((self.ev / "stage0/PREFLIGHT_PASS").exists())

    # ---------------------------------------------------------------- gates

    def test_gate_requires_a_passed_preflight(self):
        result = self.run_script("gate", "mcp")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Stage 0 has not passed", result.stderr)
        self.assertNotIn("upgrade", self.calls())

    def test_gate_passes_with_exact_identities_and_server_dry_run(self):
        self.preflight()
        for which in ("mcp", "agents"):
            with self.subTest(which):
                result = self.run_script("gate", which)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls().count("--dry-run=server"), 2)
        self.assertNotIn("REAL-UPGRADE", self.calls())

    def test_unexpected_identity_fails_even_with_the_right_count(self):
        self.preflight()
        # Same count (2 changed) but the Service changed instead of drift-mcp.
        on = [MCP_ON[0], MCP_LIVE[1], doc("Service", "ewallet-mcp", "drift-mcp", port=9090)]
        self.write("llm-agent-mcp", MCP_LIVE, on)
        result = self.run_script("gate", "mcp")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("--dry-run=server", self.calls())

    def test_missing_expected_change_fails(self):
        self.preflight()
        self.write("llm-agent", AGENTS_LIVE, AGENTS_ON[:2] + [AGENTS_LIVE[2]] + AGENTS_ON[3:])
        self.assertNotEqual(self.run_script("gate", "agents").returncode, 0)
        self.assertNotIn("--dry-run=server", self.calls())

    def test_template_failure_stops_before_dry_run_and_upgrade(self):
        self.preflight()
        result = self.run_script("upgrade", "mcp", AGI_APPROVAL=f"A1:{CAMPAIGN}", FAKE_TEMPLATE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("--dry-run=server", self.calls())
        self.assertNotIn("REAL-UPGRADE", self.calls())

    def test_server_dry_run_failure_blocks_the_upgrade(self):
        self.preflight()
        result = self.run_script("upgrade", "mcp", AGI_APPROVAL=f"A1:{CAMPAIGN}", FAKE_DRYRUN_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--dry-run=server", self.calls())
        self.assertNotIn("REAL-UPGRADE", self.calls())

    def test_missing_input_fails_before_rendering(self):
        self.preflight()
        campaign = {key: value for key, value in self.campaign.items() if key != "MCP_IMAGE"}
        result = self.run_script("gate", "mcp", campaign=campaign)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("helm --kube-context gke_test_ctx template llm-agent-mcp", self.calls())

    def test_upgrade_needs_the_campaign_approval_and_the_mcp_release_first(self):
        self.preflight()
        for approval in ("", "A1:agi-20250101T000000Z", f"A2:{CAMPAIGN}"):
            with self.subTest(approval=approval):
                result = self.run_script("upgrade", "mcp", AGI_APPROVAL=approval)
                self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("REAL-UPGRADE", self.calls())
        self.assertNotIn("template", self.calls(), "approval is checked before any render")
        agents_first = self.run_script("upgrade", "agents", AGI_APPROVAL=f"A1:{CAMPAIGN}")
        self.assertIn("upgrade the MCP release first", agents_first.stderr)
        self.assertEqual(self.run_script("upgrade", "mcp", AGI_APPROVAL=f"A1:{CAMPAIGN}").returncode, 0)
        self.assertEqual(self.run_script("upgrade", "agents", AGI_APPROVAL=f"A1:{CAMPAIGN}").returncode, 0)
        self.assertEqual(self.calls().count("REAL-UPGRADE"), 2)

    def test_live_release_changed_since_preflight_blocks_the_gate(self):
        self.preflight()
        self.write("llm-agent-mcp", MCP_LIVE + [doc("ConfigMap", "ewallet-mcp", "new")], MCP_ON)
        self.assertNotEqual(self.run_script("gate", "mcp").returncode, 0)

    # ---------------------------------------------------------------- AGI-08, data state, rollback

    def test_negative_probes_run_all_and_fail_on_any_unproven_denial(self):
        self.preflight()
        self.assertEqual(self.run_script("negative").returncode, 0)
        self.assertNotIn("REAL-", self.calls())
        self.assertNotIn("t" * 40, self.calls(), "tokens never appear in a command line")
        self.log.unlink()
        shutil.rmtree(self.ev / "stage2")
        (self.ev / "stage2").mkdir()
        result = self.run_script("negative", FAKE_PROBE_RC=json.dumps({"AGI-08-mcp-wrong-server-credential.json": 1}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls().count("--expect"), 3, "the remaining probes still ran")
        self.assertIn("FAIL AGI-08 sandbox-feature-rag credential refused by drift-mcp", (self.ev / "campaign.log").read_text())

    def test_data_state_detects_a_changed_db3_digest(self):
        self.preflight()
        self.assertEqual(self.run_script("data-state", "before").returncode, 0)
        self.assertEqual(self.run_script("data-state", "after").returncode, 0)
        changed = self.run_script("data-state", "after", FAKE_DB3_DIGEST="bbbb")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("DB 3 content changed", changed.stderr)

    def test_rollback_needs_a2_and_targets_the_preflight_revision(self):
        self.preflight()
        self.assertNotEqual(self.run_script("rollback", "mcp", AGI_APPROVAL=f"A1:{CAMPAIGN}").returncode, 0)
        self.assertNotIn("REAL-ROLLBACK", self.calls())
        result = self.run_script("rollback", "mcp", AGI_APPROVAL=f"A2:{CAMPAIGN}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REAL-ROLLBACK llm-agent-mcp 7", self.calls())

    def commit(self, path, text):
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": os.environ["PATH"]}
        target = self.git / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        subprocess.run(["git", "-C", str(self.git), "add", path], check=True, env=env)
        subprocess.run(["git", "-C", str(self.git), "commit", "-qm", path], check=True, env=env)
        return subprocess.run(["git", "-C", str(self.git), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_image_sha_may_precede_sha_only_across_non_image_changes(self):
        image = self.sha
        docs_only = self.commit("docs/handoff.md", "digests")
        campaign = {**self.campaign, "SHA": docs_only, "IMAGE_SHA": image}
        self.assertEqual(self.run_script("preflight", campaign=campaign).returncode, 0)
        result = self.run_script("__args", "mcp", campaign=campaign)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"mcpServices.identity.gitSha={image}", result.stdout.splitlines())
        changed = self.commit("ai_platform/llm_agent/x.py", "changed")
        result = self.run_script("__args", "mcp", campaign={**campaign, "SHA": changed})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image inputs changed", result.stderr)
        self.assertNotIn("__END__", result.stdout)
        result = self.run_script("__args", "mcp", campaign={**campaign, "SHA": image, "IMAGE_SHA": changed})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not an ancestor", result.stderr)

    # ---------------------------------------------------------------- agent-platform packages

    def args_for(self, package, which, **env):
        result = self.run_script("__args", which, AGI_PACKAGE=package, **env)
        return result, result.stdout.splitlines()

    def test_packages_render_their_own_overlays(self):
        self.preflight()
        cases = {
            ("p1-mcp", "mcp"): ["agi/mcp.yaml", "agi/mcp-reliability.yaml"],
            ("p2-agents", "agents"): ["agi/agents.yaml", "g3/keda.yaml", "agi/keda.yaml"],
            ("p4-agents", "agents"): ["a2aGateway.sandboxRoutes.namespace=ewallet-sandbox",
                                      "agents.coordinator.byo.sandboxSpecialists={drift-specialist,feature-rag-specialist}"],
        }
        for (package, which), wanted in cases.items():
            with self.subTest(package):
                result, args = self.args_for(package, which)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(args[-1], "__END__")
                for item in wanted:
                    self.assertTrue(any(arg.endswith(item) for arg in args), (item, args))
        result, args = self.args_for("s1-mcp", "mcp", SANDBOX_SERVER_SET="sbx1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mcpCredentialLifecycle.serverSet=sbx1", args)
        self.assertNotEqual(self.args_for("s1-mcp", "mcp")[0].returncode, 0, "the server set is required")

    def test_s1_agents_appends_the_sandbox_flow_to_the_live_flows(self):
        self.preflight()
        live = self.tmp / "private" / "llm-agent-values.yaml"
        live.write_text(yaml.safe_dump({"crossNamespace": {"flows": [{"name": "existing", "direction": "egress"}]}}))
        result, args = self.args_for("s1-agents", "agents")
        self.assertEqual(result.returncode, 0, result.stderr)
        overlay = yaml.safe_load((self.tmp / "private" / "s1-agents-flows.yaml").read_text())
        self.assertEqual([flow["name"] for flow in overlay["crossNamespace"]["flows"]],
                         ["existing", "sandbox-workers-to-mcp-gateway"])

    def test_t1_agents_appends_the_sandbox_otlp_flow_to_the_live_flows(self):
        self.preflight(AGI_PACKAGE="t1-agents", FAKE_DRIFT_TYPE="BYO")
        live = self.tmp / "private" / "llm-agent-values.yaml"
        live.write_text(yaml.safe_dump({"crossNamespace": {"flows": [{"name": "sandbox-workers-to-mcp-gateway"}]}}))
        result, args = self.args_for("t1-agents", "agents")
        self.assertEqual(result.returncode, 0, result.stderr)
        overlay = yaml.safe_load((self.tmp / "private" / "t1-agents-flows.yaml").read_text())
        flows = overlay["crossNamespace"]["flows"]
        self.assertEqual([flow["name"] for flow in flows], ["sandbox-workers-to-mcp-gateway", "sandbox-workers-to-otel-collector"])
        self.assertEqual((flows[1]["local"], flows[1]["port"], flows[1]["peer"]["serviceAccount"]), ("otel-collector", 4317, "default"))
        identities = self.run_script("__identities", "agents", AGI_PACKAGE="t1-agents").stdout.split()
        self.assertEqual(sorted(identities[i + 1] for i, arg in enumerate(identities) if arg == "--expect-added"),
                         ["AuthorizationPolicy/ewallet-agents/allow-sandbox-workers-to-otel-collector",
                          "NetworkPolicy/ewallet-agents/allow-sandbox-workers-to-otel-collector-ingress"])

    def test_tr_agents_appends_the_kagent_controller_otlp_flow_to_the_live_flows(self):
        self.preflight(AGI_PACKAGE="tr-agents", FAKE_DRIFT_TYPE="BYO")
        live = self.tmp / "private" / "llm-agent-values.yaml"
        live.write_text(yaml.safe_dump({"crossNamespace": {"flows": [{"name": "sandbox-workers-to-otel-collector"}]}}))
        result, args = self.args_for("tr-agents", "agents")
        self.assertEqual(result.returncode, 0, result.stderr)
        overlay = yaml.safe_load((self.tmp / "private" / "tr-agents-flows.yaml").read_text())
        flows = overlay["crossNamespace"]["flows"]
        self.assertEqual([flow["name"] for flow in flows], ["sandbox-workers-to-otel-collector", "kagent-controller-to-otel-collector"])
        self.assertEqual((flows[1]["direction"], flows[1]["local"], flows[1]["port"]), ("ingress", "otel-collector", 4317))
        self.assertEqual((flows[1]["peer"]["namespace"], flows[1]["peer"]["serviceAccount"]), ("kagent", "kagent-controller"))
        identities = self.run_script("__identities", "agents", AGI_PACKAGE="tr-agents").stdout.split()
        self.assertEqual(sorted(identities[i + 1] for i, arg in enumerate(identities) if arg == "--expect-added"),
                         ["AuthorizationPolicy/ewallet-agents/allow-kagent-controller-to-otel-collector",
                          "NetworkPolicy/ewallet-agents/allow-kagent-controller-to-otel-collector-ingress"])

    def test_sp_cleanup_renders_the_overlay_and_expects_the_exact_removals(self):
        self.preflight(AGI_PACKAGE="sp-cleanup", FAKE_DRIFT_TYPE="BYO")
        result, args = self.args_for("sp-cleanup", "agents")
        self.assertEqual(result.returncode, 0, result.stderr)
        files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
        self.assertEqual([Path(f).name for f in files], ["llm-agent-values.yaml", "specialists-sandbox-only.yaml"])
        self.assertNotIn("--set", args, "no image or value change beyond the overlay")
        identities = self.run_script("__identities", "agents", AGI_PACKAGE="sp-cleanup").stdout.split()
        removed = [identities[i + 1] for i, a in enumerate(identities) if a == "--expect-removed"]
        changed = [identities[i + 1] for i, a in enumerate(identities) if a == "--expect-changed"]
        self.assertEqual(len(removed), 42)
        self.assertIn("Agent/ewallet-agents/drift-specialist", removed)
        self.assertIn("Agent/ewallet-agents/feature-rag-specialist", removed)
        self.assertNotIn("Agent/ewallet-agents/fraud-coordinator", removed)
        self.assertEqual(sorted(changed), sorted([
            "Agent/ewallet-agents/fraud-coordinator", "ConfigMap/ewallet-agents/a2a-gateway-config",
            "ConfigMap/ewallet-agents/llm-agent-network-flows", "Deployment/ewallet-agents/a2a-gateway",
            "NetworkPolicy/ewallet-agents/allow-byo-modelconfig-to-kube-apiserver",
            "RoleBinding/ewallet-agents/ewallet-global-llm-reader"]))

    def test_credential_retirement_packages_render_the_final_set_and_never_roll_back(self):
        self.preflight()
        for package, which, overlay, base, changed in (
                ("cr-mcp", "mcp", "mcp-retire-legacy.yaml", "llm-agent-mcp-values.yaml",
                 ["Deployment/ewallet-mcp/drift-mcp", "Deployment/ewallet-mcp/feature-rag-mcp"]),
                ("cr-agents", "agents", "agents-retire-legacy-mcp.yaml", "llm-agent-values.yaml",
                 ["Deployment/ewallet-agents/drift-mcp"])):
            with self.subTest(package):
                result, _ = self.args_for(package, which)
                self.assertNotEqual(result.returncode, 0, "FINAL_SERVER_SET is required")
                result, args = self.args_for(package, which, FINAL_SERVER_SET="fin-20261117")
                self.assertEqual(result.returncode, 0, result.stderr)
                files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
                self.assertEqual([Path(f).name for f in files], [f"{package}-values.yaml"])
                rendered = yaml.safe_load(Path(files[0]).read_text()) or {}
                declared = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/agi" / overlay).read_text())
                for name in declared["mcpServices"]["services"]:
                    grants = ((rendered.get("mcpServices") or {}).get("services", {}).get(name) or {}).get("toolGrants") or {}
                    self.assertFalse({"drift-specialist", "feature-rag-specialist"} & set(grants), "retired grants deleted")
                    self.assertNotIn(None, grants.values())
                self.assertIn("mcpCredentialLifecycle.serverSet=fin-20261117", args)
                identities = self.run_script("__identities", which, AGI_PACKAGE=package).stdout.split()
                self.assertEqual(sorted(identities[i + 1] for i, a in enumerate(identities) if a == "--expect-changed"),
                                 changed)
                rollback = self.run_script("rollback", which, AGI_PACKAGE=package,
                                           AGI_APPROVAL=f"{package.upper()}-ROLLBACK:{CAMPAIGN}")
                self.assertNotEqual(rollback.returncode, 0)
                self.assertIn("no rollback", rollback.stderr)
        self.assertNotIn("REAL-ROLLBACK", self.log.read_text() if self.log.exists() else "")

    def test_legacy_cleanup_packages_render_the_overlay_on_the_live_values(self):
        self.preflight()
        for package, which, overlay in (("lc-agents", "agents", "agents-retire-in-release-mcp.yaml"),
                                        ("lc-mcp", "mcp", "mcp-drop-retired-client-refs.yaml")):
            with self.subTest(package):
                result, args = self.args_for(package, which)
                self.assertEqual(result.returncode, 0, result.stderr)
                files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
                self.assertEqual([Path(f).name for f in files], [f"{package}-values.yaml"])
                self.assertNotIn("--set", args, "the live values already carry the final server set")
                rendered = yaml.safe_load(Path(files[0]).read_text()) or {}
                specialists = (rendered.get("agents") or {}).get("specialists") or {}
                self.assertFalse(any("authHeaderSecrets" in (s or {}) for s in specialists.values()))
                if package == "lc-agents":
                    drift = rendered["mcpServices"]["services"]["drift-mcp"]
                    self.assertEqual((drift["retired"], drift["enabled"]), (True, False))
                    self.assertNotIn("authSecret", drift)
        identities = self.run_script("__identities", "agents", AGI_PACKAGE="lc-agents").stdout.split()
        removed = sorted(identities[i + 1] for i, a in enumerate(identities) if a == "--expect-removed")
        changed = [identities[i + 1] for i, a in enumerate(identities) if a == "--expect-changed"]
        self.assertEqual(len(removed), 8)
        self.assertTrue(all(r.startswith(("Deployment/ewallet-agents/drift-mcp", "Service/ewallet-agents/drift-mcp",
                                          "PodDisruptionBudget/ewallet-agents/drift-mcp", "ServiceAccount/ewallet-agents/drift-mcp"))
                            or "drift-mcp" in r.split("/")[2] for r in removed), removed)
        self.assertFalse([r for r in removed if "/ewallet-mcp/" in r], "the active ewallet-mcp server is never touched")
        self.assertEqual(changed, ["ConfigMap/ewallet-agents/llm-agent-network-flows"])
        self.assertEqual(self.run_script("__identities", "mcp", AGI_PACKAGE="lc-mcp").stdout.split(), [],
                         "lc-mcp expects exactly no object change")

    def test_feature_rag_retirement_package_renders_its_overlay_and_expects_only_exclusive_objects(self):
        self.preflight()
        result, args = self.args_for("lf-agents", "agents")
        self.assertEqual(result.returncode, 0, result.stderr)
        files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
        self.assertEqual([Path(f).name for f in files], ["lf-agents-values.yaml"])
        self.assertNotIn("--set", args)
        rendered = yaml.safe_load(Path(files[0]).read_text()) or {}
        services = rendered["mcpServices"]["services"]
        self.assertEqual((services["feature-rag-mcp"]["retired"], services["feature-rag-mcp"]["enabled"]), (True, False))
        identities = self.run_script("__identities", "agents", AGI_PACKAGE="lf-agents").stdout.split()
        removed = sorted(identities[i + 1] for i, a in enumerate(identities) if a == "--expect-removed")
        changed = [identities[i + 1] for i, a in enumerate(identities) if a == "--expect-changed"]
        self.assertEqual(len(removed), 9)
        self.assertTrue(all("feature-rag-mcp" in r.split("/")[2] for r in removed), removed)
        self.assertFalse([r for r in removed if "/ewallet-mcp/" in r or "allow-ewallet-mcp-" in r],
                         "the active ewallet-mcp server and its peer policies are never touched")
        self.assertFalse([r for r in removed if r.startswith(("RemoteMCPServer/", "ConfigMap/"))
                          or "cross-namespace-egress" in r], "the gateway route and RemoteMCPServer stay")
        self.assertEqual({r.split("/")[1] for r in removed}, {"ewallet-agents", "feature-store", "fraud-serving"})
        self.assertEqual(changed, ["ConfigMap/ewallet-agents/llm-agent-network-flows"])
        upgrade = self.run_script("upgrade", "agents", AGI_PACKAGE="lf-agents", AGI_APPROVAL=f"LC-AGENTS:{CAMPAIGN}")
        self.assertNotEqual(upgrade.returncode, 0)
        self.assertIn("approval LF-AGENTS", upgrade.stderr)
        self.assertNotIn("REAL-UPGRADE", self.calls())

    # P0.7 on the Sandbox-only release (replay-20261118T161231Z: lf-agents preflight failed P0.7 because lf-* was
    # missing from the BYO packages, so it expected a Declarative drift-specialist that sp-cleanup had removed).
    SANDBOX_ONLY = {"FAKE_DRIFT_ABSENT": "1"}

    def sandbox_values(self) -> dict:
        values = self.tmp / "sandbox-values.yaml"
        values.write_text(yaml.safe_dump({"agents": {"specialists": {
            "drift-specialist": {"runtime": "sandbox"}, "feature-rag-specialist": {"runtime": "sandbox"}}}}))
        return {"FAKE_VALUES": str(values)}

    def assert_p07_fails(self, **env):
        result = self.run_script("preflight", **env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.ev / "stage0/PREFLIGHT_PASS").exists())
        self.assertIn("FAIL P0.7", (self.ev / "campaign.log").read_text())
        self.assertNotRegex(self.calls(), r"REAL-|kubectl .*(apply|create|delete|patch|scale)")

    def test_lf_agents_preflight_accepts_the_sandbox_only_release(self):
        self.preflight(AGI_PACKAGE="lf-agents", **self.SANDBOX_ONLY, **self.sandbox_values())
        self.assertIn("get agents.kagent.dev fraud-coordinator", self.preflight_calls)
        self.assertIn("-n ewallet-sandbox get sandboxagents.kagent.dev feature-rag-specialist drift-specialist",
                      self.preflight_calls)
        self.assertNotRegex(self.preflight_calls, r"REAL-|kubectl .*(apply|create|delete|patch|scale)")
        gate = self.run_script("gate", "agents", AGI_PACKAGE="lf-agents")   # the official gate now runs
        self.assertNotIn("Stage 0 has not passed", gate.stderr)

    def test_lc_agents_and_earlier_packages_keep_their_p07_behaviour(self):
        self.preflight(AGI_PACKAGE="lc-agents", **self.SANDBOX_ONLY, **self.sandbox_values())
        self.preflight(AGI_PACKAGE="lc-agents", FAKE_DRIFT_TYPE="BYO")      # before sp-cleanup: BYO Agent CR
        self.preflight()                                                    # agi: Declarative before the campaign
        self.assert_p07_fails(FAKE_DRIFT_TYPE="BYO")                        # agi on a BYO drift-specialist

    def test_lf_agents_rejects_every_other_architecture(self):
        sandbox = self.sandbox_values()
        cases = {
            "Deployment Specialists, BYO Agent CR": {"FAKE_DRIFT_TYPE": "BYO"},
            "Declarative drift-specialist": {"FAKE_DRIFT_TYPE": "Declarative"},
            "legacy drift-specialist Agent CR still live": {**sandbox, "FAKE_DRIFT_TYPE": "BYO"},
            "Declarative Coordinator": {**sandbox, **self.SANDBOX_ONLY, "FAKE_COORDINATOR_TYPE": "Declarative"},
            "SandboxAgents missing": {**sandbox, **self.SANDBOX_ONLY, "FAKE_SANDBOXAGENTS_MISSING": "1"},
        }
        for label, env in cases.items():
            with self.subTest(label):
                self.assert_p07_fails(AGI_PACKAGE="lf-agents", **env)

    def test_gate_and_rollback_refuse_manifests_that_select_a_retired_credential(self):
        self.preflight()
        on = self.fake / "llm-agent-mcp-on.yaml"
        on.write_text(on.read_text() + "\n# name: mcp-token-drift-specialist-drift-mcp\n")
        result = self.run_script("upgrade", "mcp", AGI_APPROVAL=f"A1:{CAMPAIGN}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("render references a retired MCP credential", result.stderr)
        self.assertNotIn("REAL-UPGRADE", self.calls())
        live = self.fake / "llm-agent-mcp-live.yaml"   # the rollback target revision's manifest
        live.write_text(live.read_text() + "\n# env secretKeyRef name: mcp-auth-drift-mcp-sbx-20261115\n")
        result = self.run_script("rollback", "mcp", AGI_APPROVAL=f"A2:{CAMPAIGN}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revision 7 references a retired MCP credential", result.stderr)
        self.assertNotIn("REAL-ROLLBACK", self.calls())

    def test_a_package_refuses_the_other_release_and_unknown_packages(self):
        self.preflight()
        result = self.run_script("gate", "agents", AGI_PACKAGE="p1-mcp")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changes the mcp release", result.stderr)
        result = self.run_script("gate", "mcp", AGI_PACKAGE="p9-everything")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGI_PACKAGE must be one of", result.stderr)

    def test_p4_gate_checks_its_exact_identities_and_needs_its_own_approval(self):
        on = [doc("Agent", "ewallet-agents", "fraud-coordinator", version="sbx")] + AGENTS_LIVE[1:] + [
            doc("ConfigMap", "ewallet-agents", "a2a-gateway-config", routes="sbx"),
            doc("Deployment", "ewallet-agents", "a2a-gateway", checksum="new")]
        live = AGENTS_LIVE + [doc("ConfigMap", "ewallet-agents", "a2a-gateway-config", routes="old"),
                              doc("Deployment", "ewallet-agents", "a2a-gateway", checksum="old")]
        self.write("llm-agent", live, on)
        env = {"AGI_PACKAGE": "p4-agents", "FAKE_ON_MATCH": "sandboxRoutes", "FAKE_DRIFT_TYPE": "BYO"}
        self.preflight(**env)
        result = self.run_script("gate", "agents", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for token in (f"A1:{CAMPAIGN}", f"P2-AGENTS:{CAMPAIGN}"):
            with self.subTest(token):
                refused = self.run_script("upgrade", "agents", AGI_APPROVAL=token, **env)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(f"AGI_APPROVAL=P4-AGENTS:{CAMPAIGN}", refused.stderr)
        self.assertNotIn("REAL-UPGRADE", self.calls())
        result = self.run_script("upgrade", "agents", AGI_APPROVAL=f"P4-AGENTS:{CAMPAIGN}", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REAL-UPGRADE llm-agent", self.calls())

    def test_image_rolls_render_the_live_values_and_the_images_only(self):
        self.preflight(AGI_PACKAGE="img-agents", FAKE_DRIFT_TYPE="BYO")
        cases = {
            ("img-mcp", "mcp"): ("llm-agent-mcp-values.yaml", ["mcpServices.image=", "mcpServices.identity.imageDigest=",
                                                                "mcpServices.identity.gitSha="]),
            ("img-agents", "agents"): ("llm-agent-values.yaml", ["agents.coordinator.byo.image=",
                                                                  "agents.specialists.feature-rag-specialist.byo.image=",
                                                                  "agents.specialists.drift-specialist.byo.image="]),
            ("img-coordinator", "agents"): ("llm-agent-values.yaml", ["agents.coordinator.byo.image="]),
        }
        for (package, which), (values, sets) in cases.items():
            with self.subTest(package):
                result, args = self.args_for(package, which)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(args[-1], "__END__")
                files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
                self.assertEqual([Path(item).name for item in files], [values], "no overlay beyond the live values")
                assigned = sorted(args[i + 1].split("=")[0] + "=" for i, arg in enumerate(args) if arg == "--set")
                self.assertEqual(assigned, sorted(sets))
        self.assertNotEqual(self.run_script("__args", "agents", AGI_PACKAGE="img-mcp").returncode, 0,
                            "img-mcp does not change the agents release")

    def test_image_rolls_expect_only_the_image_bearing_objects(self):
        cases = {
            ("img-mcp", "mcp"): ["Deployment/ewallet-mcp/drift-mcp", "Deployment/ewallet-mcp/feature-rag-mcp"],
            ("img-agents", "agents"): ["Agent/ewallet-agents/drift-specialist", "Agent/ewallet-agents/feature-rag-specialist",
                                       "Agent/ewallet-agents/fraud-coordinator"],
            ("img-coordinator", "agents"): ["Agent/ewallet-agents/fraud-coordinator"],
        }
        for (package, which), objects in cases.items():
            with self.subTest(package):
                result = self.run_script("__identities", which, AGI_PACKAGE=package)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = result.stdout.split()
                self.assertEqual(sorted(args[i + 1] for i, arg in enumerate(args) if arg == "--expect-changed"), objects)
                self.assertFalse([arg for arg in args if arg in ("--expect-added", "--expect-removed")])

    def test_image_roll_preflight_expects_the_byo_drift_specialist(self):
        self.assertNotEqual(self.run_script("preflight", AGI_PACKAGE="img-agents").returncode, 0)
        self.assertEqual(self.run_script("preflight", AGI_PACKAGE="img-agents", FAKE_DRIFT_TYPE="BYO").returncode, 0)

    def test_p4_preflight_expects_the_byo_drift_specialist(self):
        result = self.run_script("preflight", AGI_PACKAGE="p4-agents")
        self.assertNotEqual(result.returncode, 0, "drift-specialist is still Declarative")
        self.assertEqual(self.run_script("preflight", AGI_PACKAGE="p4-agents", FAKE_DRIFT_TYPE="BYO").returncode, 0)

    def test_p2_rollback_deletes_the_scaledobjects_first_and_needs_the_hpas_back(self):
        env = {"AGI_PACKAGE": "p2-agents"}
        self.preflight(**env)
        self.assertNotEqual(self.run_script("rollback", "agents", AGI_APPROVAL=f"A2:{CAMPAIGN}", **env).returncode, 0)
        result = self.run_script("rollback", "agents", AGI_APPROVAL=f"P2-AGENTS-ROLLBACK:{CAMPAIGN}", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        deletes = [calls.index(f"delete scaledobject {agent}") for agent in
                   ("fraud-coordinator", "feature-rag-specialist", "drift-specialist")]
        self.assertLess(max(deletes), calls.index("REAL-ROLLBACK llm-agent 7"))
        self.assertIn("get hpa drift-specialist", calls)
        missing = self.run_script("rollback", "agents", AGI_APPROVAL=f"P2-AGENTS-ROLLBACK:{CAMPAIGN}",
                                  FAKE_HPA_MISSING="1", **env)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("HPA ewallet-agents/fraud-coordinator is missing", missing.stderr)


class HelmObjectDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agi-diff-"))
        (self.tmp / "live.yaml").write_text(yaml.safe_dump_all(MCP_LIVE))
        (self.tmp / "new.yaml").write_text(yaml.safe_dump_all(MCP_ON))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def diff(self, *args):
        return subprocess.run([sys.executable, str(DIFF), str(self.tmp / "live.yaml"), str(self.tmp / "new.yaml"), *args],
                              capture_output=True, text=True)

    def test_exact_identities_pass_and_anything_else_fails(self):
        exact = ("--expect-identities", "--expect-changed", "Deployment/ewallet-mcp/feature-rag-mcp",
                 "--expect-changed", "Deployment/ewallet-mcp/drift-mcp")
        self.assertEqual(self.diff(*exact).returncode, 0)
        self.assertIn("RESULT: PASS", self.diff(*exact).stdout)
        wrong = ("--expect-identities", "--expect-changed", "Deployment/ewallet-mcp/feature-rag-mcp",
                 "--expect-changed", "Service/ewallet-mcp/drift-mcp")
        result = self.diff(*wrong)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected changed: Deployment/ewallet-mcp/drift-mcp", result.stderr)
        self.assertIn("expected changed but not changed: Service/ewallet-mcp/drift-mcp", result.stderr)
        self.assertEqual(self.diff("--expect-identities").returncode, 1)
        self.assertEqual(self.diff("--expect-objects", "2").returncode, 0, "count-only mode keeps its old meaning")

    def test_unreadable_manifest_is_an_error(self):
        (self.tmp / "new.yaml").write_text("kind: [unclosed")
        self.assertEqual(self.diff("--expect-objects", "0").returncode, 2)


if __name__ == "__main__":
    unittest.main()
