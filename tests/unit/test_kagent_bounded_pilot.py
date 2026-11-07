from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "infra" / "kubernetes" / "kagent-pilot"


def load_documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def load_documents_from_text(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


class KagentBoundedPilotTest(unittest.TestCase):
    def test_versions_are_pinned(self) -> None:
        text = (PILOT / "versions.env").read_text()
        self.assertRegex(text, r"(?m)^KAGENT_VERSION=0\.10\.3$")
        self.assertRegex(text, r"(?m)^SUBSTRATE_VERSION=0\.0\.9$")
        self.assertNotRegex(text.lower(), r"latest")

    def test_substrate_uses_exact_gke_jwt_issuer(self) -> None:
        values = yaml.safe_load((PILOT / "values" / "substrate.yaml").read_text())
        self.assertEqual(values["auth"]["mode"], "jwt")
        self.assertEqual(
            values["auth"]["jwt"]["issuer"],
            "https://container.googleapis.com/v1/projects/ewallet-data-ai-platform-hg26/locations/asia-southeast1-b/clusters/ewallet-dev",
        )
        self.assertEqual(values["valkey"]["replicas"], 6)
        self.assertEqual(values["valkey"]["storageSize"], "256Mi")
        self.assertEqual(values["rustfs"]["storageSize"], "256Mi")

    def test_kagent_disables_unneeded_components_and_scopes_rbac(self) -> None:
        values = yaml.safe_load((PILOT / "values" / "kagent.yaml").read_text())
        self.assertEqual(values["rbac"]["namespaces"], ["kagent-pilot"])
        self.assertEqual(values["controller"]["watchNamespaces"], ["kagent-pilot"])
        self.assertTrue(values["controller"]["substrate"]["enabled"])
        self.assertEqual(values["substrateWorkerPool"]["replicas"], 1)
        self.assertEqual(values["ui"]["replicas"], 0)
        self.assertIsNone(values["providers"])
        bundled_db = values["database"]["postgres"]["bundled"]
        self.assertEqual(bundled_db["storage"], "256Mi")
        self.assertEqual(bundled_db["resources"]["requests"]["memory"], "128Mi")
        for component in (
            "kmcp",
            "kagent-tools",
            "grafana-mcp",
            "k8s-agent",
            "kgateway-agent",
            "istio-agent",
            "promql-agent",
            "observability-agent",
            "argo-rollouts-agent",
            "helm-agent",
            "cilium-policy-agent",
            "cilium-manager-agent",
            "cilium-debug-agent",
        ):
            self.assertFalse(values[component]["enabled"], component)

    def test_runtime_overlay_preserves_production_watch_and_isolates_worker(self) -> None:
        values = yaml.safe_load(
            (PILOT / "values" / "kagent-runtime-overlay.yaml").read_text()
        )
        self.assertEqual(
            values["controller"]["watchNamespaces"],
            ["ewallet-agents", "kagent-pilot"],
        )
        self.assertEqual(
            values["controller"]["substrate"]["defaultWorkerPool"]["namespace"],
            "kagent-pilot",
        )
        self.assertEqual(values["substrateWorkerPool"]["replicas"], 1)
        self.assertIn("@sha256:", values["substrateWorkerPool"]["ateomImage"])

    def test_mock_is_internal_deterministic_and_hardened(self) -> None:
        docs = load_documents(PILOT / "manifests" / "mock-openai.yaml")
        by_kind = {doc["kind"]: doc for doc in docs}
        service = by_kind["Service"]
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        deployment = by_kind["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("@sha256:", container["image"])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/healthz")
        security = container["securityContext"]
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        script = next(doc for doc in docs if doc["kind"] == "ConfigMap")["data"]["server.py"]
        self.assertIn("KAGENT_PILOT_OK", script)
        self.assertIn("/v1/chat/completions", script)
        compile(script, "mock-openai/server.py", "exec")
        policy = next(doc for doc in docs if doc["kind"] == "NetworkPolicy")
        self.assertEqual(
            policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"],
            {"ate.dev/worker-pool": "kagent-pilot"},
        )

    def test_model_and_sandbox_form_the_intended_path(self) -> None:
        model_docs = load_documents(PILOT / "manifests" / "model-config.yaml")
        model = next(doc for doc in model_docs if doc["kind"] == "ModelConfig")
        self.assertEqual(model["apiVersion"], "kagent.dev/v1alpha2")
        self.assertEqual(model["spec"]["provider"], "OpenAI")
        self.assertEqual(model["spec"]["apiKeySecret"], "pilot-mock-openai")
        self.assertNotIn("apiKeySecretRef", model["spec"])
        self.assertEqual(
            model["spec"]["openAI"]["baseUrl"],
            "http://mock-openai.kagent-pilot.svc.cluster.local:8080/v1",
        )
        agent = load_documents(PILOT / "manifests" / "sandbox-agent.yaml")[0]
        self.assertEqual(agent["kind"], "SandboxAgent")
        self.assertEqual(agent["spec"]["declarative"]["modelConfig"], "pilot-mock-openai")
        self.assertEqual(agent["spec"]["declarative"]["tools"], [])
        self.assertEqual(agent["spec"]["substrate"]["workerPoolRef"]["name"], "kagent-pilot")
        self.assertEqual(
            agent["spec"]["sandbox"]["network"]["allowedDomains"],
            ["mock-openai.kagent-pilot.svc.cluster.local"],
        )
        self.assertEqual(agent["spec"]["declarative"]["runtime"], "go")
        self.assertFalse(agent["spec"]["declarative"]["executeCodeBlocks"])
        self.assertNotIn("RemoteMCPServer", (PILOT / "manifests" / "sandbox-agent.yaml").read_text())

    def test_worker_egress_is_allowlisted_and_has_no_public_cidr(self) -> None:
        policy = load_documents(PILOT / "manifests" / "worker-egress.yaml")[0]
        self.assertEqual(policy["kind"], "NetworkPolicy")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"ate.dev/worker-pool": "kagent-pilot"},
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
        egress = policy["spec"]["egress"]
        self.assertTrue(egress)
        serialized = yaml.safe_dump(egress)
        self.assertNotIn("0.0.0.0/0", serialized)
        self.assertNotIn("169.254.169.254", serialized)
        self.assertIn("34.118.224.10/32", serialized)
        self.assertIn("mock-openai", serialized)
        self.assertIn("kube-dns", serialized)
        self.assertIn("atenet-router", serialized)
        for rule in egress:
            self.assertIn("to", rule)
            for peer in rule["to"]:
                if "ipBlock" in peer:
                    self.assertEqual(peer["ipBlock"], {"cidr": "34.118.224.10/32"})
                    continue
                self.assertIn("podSelector", peer)
                if "namespaceSelector" in peer:
                    self.assertIn(
                        "kubernetes.io/metadata.name",
                        peer["namespaceSelector"]["matchLabels"],
                    )

    def test_pilot_never_references_production_credentials_or_tools(self) -> None:
        manifest_text = "\n".join(
            path.read_text() for path in sorted((PILOT / "manifests").glob("*.yaml"))
        ).lower()
        for forbidden in (
            "mcp-token-",
            "mcp-auth-",
            "feast-online-redis",
            "fraud-scoring",
            "feature_lookup",
            "knowledge_search",
            "drift_detection",
        ):
            self.assertNotIn(forbidden, manifest_text)

    def test_no_external_service_or_cluster_admin(self) -> None:
        manifest_text = "\n".join(
            path.read_text() for path in sorted((PILOT / "manifests").glob("*.yaml"))
        )
        self.assertNotIn("type: LoadBalancer", manifest_text)
        self.assertNotIn("kind: Ingress", manifest_text)
        self.assertNotIn("cluster-admin", manifest_text)
        self.assertNotIn("kind: ClusterRoleBinding", manifest_text)

    def test_substrate_post_renderer_bounds_every_workload_container(self) -> None:
        rendered = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ate-api-server-deployment
spec:
  template:
    spec:
      initContainers:
        - name: init
          image: example.invalid/init@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      containers:
        - name: ate-api-server
          image: example.invalid/api@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
---
apiVersion: v1
kind: Service
metadata:
  name: untouched
spec: {}
"""
        bounded = subprocess.run(
            [str(PILOT / "scripts" / "substrate_resource_post_renderer.py")],
            input=rendered,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        workloads = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
        for document in yaml.safe_load_all(bounded):
            if not document or document.get("kind") not in workloads:
                continue
            pod_spec = document["spec"]["template"]["spec"]
            for section in ("initContainers", "containers"):
                for container in pod_spec.get(section, []):
                    with self.subTest(
                        workload=document["metadata"]["name"], container=container["name"]
                    ):
                        self.assertIn("requests", container["resources"])
                        self.assertIn("limits", container["resources"])

    def test_substrate_post_renderer_pins_every_runtime_image(self) -> None:
        # This deterministic fixture protects fail-closed pinning in clean
        # checkouts without requiring a network pull of the chart.
        rendered = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: ate-controller}
spec:
  template:
    spec:
      containers:
        - {name: ate-controller, image: ghcr.io/kagent-dev/substrate/atecontroller:v0.0.9}
        - {name: already-pinned, image: example.invalid/approved@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}
"""
        bounded = subprocess.run(
            [str(PILOT / "scripts" / "substrate_resource_post_renderer.py")],
            input=rendered,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        images = [
            container["image"]
            for document in yaml.safe_load_all(bounded)
            if document
            for container in document["spec"]["template"]["spec"]["containers"]
        ]
        self.assertTrue(all("@sha256:" in image for image in images))

    def test_substrate_post_renderer_rejects_unknown_mutable_image(self) -> None:
        rendered = """
apiVersion: batch/v1
kind: Job
metadata: {name: unreviewed}
spec:
  template:
    spec:
      containers:
        - {name: unknown, image: example.invalid/new:latest}
"""
        result = subprocess.run(
            [str(PILOT / "scripts" / "substrate_resource_post_renderer.py")],
            input=rendered,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreviewed mutable Substrate image", result.stderr)

    VALKEY_STS = """
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: valkey-cluster}
spec:
  replicas: 6
  template:
    spec:
      containers:
        - name: valkey
          image: valkey/valkey:8.0
          command: [valkey-server, /etc/valkey/valkey.conf]
"""

    def render_substrate(self, text: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(PILOT / "scripts" / "substrate_resource_post_renderer.py")],
            input=text, check=False, capture_output=True, text=True,
        )

    def test_valkey_announces_its_pod_ip_and_gets_a_reconciler(self) -> None:
        result = self.render_substrate(self.VALKEY_STS)
        self.assertEqual(result.returncode, 0, result.stderr)
        [sts] = load_documents_from_text(result.stdout)
        containers = {c["name"]: c for c in sts["spec"]["template"]["spec"]["containers"]}
        valkey, reconciler = containers["valkey"], containers["cluster-address-reconciler"]
        self.assertEqual(valkey["command"][-2:], ["--cluster-announce-ip", "$(POD_IP)"])
        self.assertEqual(valkey["env"][0]["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
        self.assertEqual(valkey["readinessProbe"]["exec"]["command"][-1], "ping")
        self.assertEqual(reconciler["image"], valkey["image"])
        self.assertIn("@sha256:", reconciler["image"])
        security = reconciler["securityContext"]
        self.assertTrue(security["runAsNonRoot"] and security["readOnlyRootFilesystem"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertIn("limits", reconciler["resources"])
        script = reconciler["command"][-1]
        for forbidden in ("reset", "flush", "addslots", "delslots", "setslot", "forget", "reshard", "nodes.conf"):
            self.assertNotIn(forbidden, script.lower())
        subprocess.run(["sh", "-n"], input=script, check=True, text=True)

    def test_valkey_patch_fails_closed_on_upstream_changes(self) -> None:
        for change in ("command: [valkey-server, /etc/valkey/valkey.conf, --port, '6380']",
                       "command: [valkey-server, /etc/valkey/valkey.conf]\n          args: [--x]"):
            with self.subTest(change=change):
                text = self.VALKEY_STS.replace("command: [valkey-server, /etc/valkey/valkey.conf]", change)
                result = self.render_substrate(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("G5-R", result.stderr)

    def test_ca_bundle_keeps_its_install_form_across_upgrades(self) -> None:
        pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        for leading in (1, 2, 3):
            config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "ateapi-ca"},
                      "data": {"ca.crt": "\n" * leading + pem}}
            result = self.render_substrate(yaml.safe_dump(config))
            self.assertEqual(result.returncode, 0, result.stderr)
            [rendered] = load_documents_from_text(result.stdout)
            self.assertEqual(rendered["data"]["ca.crt"], "\n" + pem)

    def test_reconciler_meets_only_known_members_with_a_bounded_retry(self) -> None:
        result = self.render_substrate(self.VALKEY_STS)
        [sts] = load_documents_from_text(result.stdout)
        script = next(c for c in sts["spec"]["template"]["spec"]["containers"]
                      if c["name"] == "cluster-address-reconciler")["command"][-1]
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, log = Path(tmp) / "bin", Path(tmp) / "calls.log"
            bin_dir.mkdir()
            # Peer 1 moved (known id, stale address); peer 2 is current; peer 3 has an unknown id.
            (bin_dir / "getent").write_text(
                "#!/bin/sh\ncase \"$2\" in valkey-cluster-0.*) echo 10.0.0.10;; valkey-cluster-1.*) echo 10.0.0.11;;"
                " valkey-cluster-2.*) echo 10.0.0.12;; valkey-cluster-3.*) echo 10.0.0.13;; esac\n")
            (bin_dir / "valkey-cli").write_text(f"""#!/bin/sh
echo "$*" >> {log}
case "$*" in
  "-h 127.0.0.1 ping") echo PONG;;
  "-h 127.0.0.1 cluster nodes") printf '%s\\n' \
    "aaa 10.0.0.10:6379@16379 myself,master - 0 0 1 connected 0-100" \
    "bbb 10.9.9.9:6379@16379 master - 0 0 2 connected 101-200" \
    "ccc 10.0.0.12:6379@16379 slave bbb 0 0 2 connected";;
  "-h 10.0.0.11 cluster myid") echo bbb;;
  "-h 10.0.0.12 cluster myid") echo ccc;;
  "-h 10.0.0.13 cluster myid") echo zzz;;
  *"cluster meet"*) echo OK;;
esac
""")
            for tool in ("getent", "valkey-cli"):
                (bin_dir / tool).chmod(0o755)
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "POD_IP": "10.0.0.10", "POD_NAMESPACE": "ate-system",
                   "VALKEY_SERVICE": "valkey-cluster-service", "VALKEY_REPLICAS": "4", "MAX_MEET_ATTEMPTS": "2"}
            # Speed the loop up and stop it after a few passes.
            fast = script.replace("sleep 15", "sleep 0.05").replace("attempts=/tmp/meet-attempts", f"attempts={tmp}/a")
            subprocess.run(["timeout", "1", "sh", "-c", fast], env=env, check=False, capture_output=True, text=True)
            calls = log.read_text().splitlines()
        meets = [c for c in calls if "cluster meet" in c]
        self.assertEqual(set(meets), {"-h 127.0.0.1 cluster meet 10.0.0.11 6379"})
        self.assertEqual(len(meets), 2)  # bounded by MAX_MEET_ATTEMPTS

    def test_kagent_runtime_post_renderer_isolates_env_source_rbac(self) -> None:
        rendered = """
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: kagent-ate-api-env-sources, namespace: ewallet-agents}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: kagent-ate-api-env-sources, namespace: ewallet-agents}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: kagent-ate-api-env-sources, namespace: kagent}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: kagent-ate-api-env-sources, namespace: kagent}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: kagent-ate-api-env-sources, namespace: kagent-pilot}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: kagent-ate-api-env-sources, namespace: kagent-pilot}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: kagent-postgresql, namespace: kagent}
spec:
  template:
    spec: {containers: [{name: postgresql, image: example.invalid/postgres@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}]}
---
apiVersion: ate.dev/v1alpha1
kind: WorkerPool
metadata: {name: kagent-pilot, namespace: kagent}
spec: {replicas: 1}
"""
        result = subprocess.run(
            [str(PILOT / "scripts" / "kagent_runtime_post_renderer.py")],
            input=rendered,
            check=True,
            capture_output=True,
            text=True,
        )
        documents = load_documents_from_text(result.stdout)
        identities = {
            (
                document["kind"],
                document.get("metadata", {}).get("namespace", ""),
                document.get("metadata", {}).get("name", ""),
            )
            for document in documents
        }
        self.assertNotIn(
            ("Role", "ewallet-agents", "kagent-ate-api-env-sources"), identities
        )
        self.assertNotIn(
            ("RoleBinding", "ewallet-agents", "kagent-ate-api-env-sources"), identities
        )
        self.assertNotIn(("Role", "kagent", "kagent-ate-api-env-sources"), identities)
        self.assertNotIn(("RoleBinding", "kagent", "kagent-ate-api-env-sources"), identities)
        self.assertIn(("Role", "kagent-pilot", "kagent-ate-api-env-sources"), identities)
        self.assertIn(("RoleBinding", "kagent-pilot", "kagent-ate-api-env-sources"), identities)
        self.assertIn(("WorkerPool", "kagent-pilot", "kagent-pilot"), identities)
        postgres = next(document for document in documents if document["kind"] == "Deployment")
        self.assertEqual(
            postgres["spec"]["template"]["spec"]["nodeSelector"],
            {"ewallet.io/g3-llm": "true"},
        )

    def test_kagent_runtime_post_renderer_fails_on_unexpected_rbac_shape(self) -> None:
        rendered = """
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: kagent-ate-api-env-sources, namespace: kagent}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: kagent-postgresql, namespace: kagent}
spec: {template: {spec: {containers: []}}}
"""
        result = subprocess.run(
            [str(PILOT / "scripts" / "kagent_runtime_post_renderer.py")],
            input=rendered,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected exactly the pilot env-source", result.stderr)

    def test_privileged_psa_is_confined_to_pilot_namespace(self) -> None:
        namespace = load_documents(PILOT / "manifests" / "namespace.yaml")[0]
        self.assertEqual(namespace["metadata"]["name"], "kagent-pilot")
        labels = namespace["metadata"]["labels"]
        self.assertEqual(labels["pod-security.kubernetes.io/enforce"], "privileged")
        for path in (PILOT / "manifests").glob("*.yaml"):
            for document in load_documents(path):
                metadata = document.get("metadata", {})
                if document.get("kind") != "Namespace" and metadata.get("namespace"):
                    self.assertEqual(metadata["namespace"], "kagent-pilot", path.name)

    def test_render_audit_fails_closed_on_cluster_scope_and_privilege(self) -> None:
        rendered = """
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: pilot-cluster-role
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: atelet
  namespace: ate-system
spec:
  template:
    spec:
      volumes:
        - name: runtime
          hostPath: {path: /run/ateom}
      containers:
        - name: atelet
          securityContext: {privileged: true}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "substrate.yaml").write_text(rendered)
            result = subprocess.run(
                [
                    "python3",
                    str(PILOT / "scripts" / "audit_render.py"),
                    str(root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["verdict"], "BLOCKED_PENDING_OWNER_APPROVAL")
        self.assertEqual(report["clusterScoped"][0]["kind"], "ClusterRole")
        self.assertEqual(
            report["privilegedWorkloads"][0]["reasons"],
            ["hostPath:runtime", "privileged:atelet"],
        )


if __name__ == "__main__":
    unittest.main()
