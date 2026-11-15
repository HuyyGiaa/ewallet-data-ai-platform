"""Exact diffs of the sandbox watch packages and the generated package overlays."""

from __future__ import annotations

import copy
import unittest

from ops.llm_agent import agi_packages
from ops.llm_agent import sandbox_watch_diff as watch


def obj(kind, namespace, name, **body):
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, **body}


def keda(watch_ns):
    deployments = {(d, watch_ns) for d in ("keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks")}
    return {("Deployment", "keda", name): obj("Deployment", "keda", name, spec={"template": {"spec": {"containers": [
        {"name": "c", "env": [{"name": "WATCH_NAMESPACE", "value": value}, {"name": "OTHER", "value": "x"}]}]}}})
        for name, value in deployments}


def kagent(watch_ns, checksum):
    return {("ConfigMap", "kagent", "kagent-controller"): obj("ConfigMap", "kagent", "kagent-controller",
                                                            data={"WATCH_NAMESPACES": watch_ns, "OTHER": "y"}),
            ("Deployment", "kagent", "kagent-controller"): obj("Deployment", "kagent", "kagent-controller", spec={
                "template": {"metadata": {"annotations": {"checksum/configmap": checksum}}, "spec": {"containers": []}}})}


class WatchDiffTests(unittest.TestCase):
    def test_keda_candidate_is_exactly_the_reviewed_change(self):
        base = keda("ewallet-agents,ml-platform")
        cand = {**keda("ewallet-agents,ml-platform,ewallet-sandbox"),
                ("RoleBinding", "ewallet-sandbox", "keda-operator"): obj("RoleBinding", "ewallet-sandbox", "keda-operator")}
        self.assertEqual(watch.candidate("keda", base, cand), [])
        extra = copy.deepcopy(cand)
        extra[("Deployment", "keda", "keda-operator")]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "z"
        self.assertTrue(watch.candidate("keda", base, extra), "a second env change is not the reviewed diff")
        outside = {**cand, ("RoleBinding", "ewallet-agents", "x"): obj("RoleBinding", "ewallet-agents", "x")}
        self.assertTrue(watch.candidate("keda", base, outside))
        wrong = {**keda("ewallet-sandbox"), ("RoleBinding", "ewallet-sandbox", "keda-operator"): obj("RoleBinding", "ewallet-sandbox", "keda-operator")}
        self.assertTrue(any("expected" in p for p in watch.candidate("keda", base, wrong)), "existing namespaces must stay")

    def test_kagent_candidate_is_exactly_the_reviewed_change(self):
        base = kagent("ewallet-agents,kagent-pilot", "a")
        added = {key: obj(key[0], key[1], key[2]) for key in watch.EXPECTED["kagent"]["added"]}
        cand = {**kagent("ewallet-agents,kagent-pilot,ewallet-sandbox", "b"), **added}
        self.assertEqual(watch.candidate("kagent", base, cand), [])
        missing = {k: v for k, v in cand.items() if k[2] != "kagent-ate-api-env-sources"}
        self.assertTrue(watch.candidate("kagent", base, missing))
        removed = dict(cand)
        base_extra = {**base, ("Role", "ewallet-agents", "kagent-writer-role"): obj("Role", "ewallet-agents", "kagent-writer-role")}
        self.assertTrue(any("removed" in p for p in watch.candidate("kagent", base_extra, removed)))

    def test_identity_reports_every_kind_of_difference(self):
        live = kagent("a", "1")
        self.assertEqual(watch.identity(live, copy.deepcopy(live)), [])
        problems = watch.identity(live, {**kagent("b", "1"), ("Role", "x", "y"): obj("Role", "x", "y")})
        self.assertTrue(any("only rendered" in p for p in problems) and any("differs" in p for p in problems))


class PackageOverlayTests(unittest.TestCase):
    def test_append_flow_keeps_the_live_flows(self):
        live = {"crossNamespace": {"flows": [{"name": "a"}, {"name": "b"}]}, "other": 1}
        overlay = agi_packages.append_flow(live, "sandbox-mcp-gateway")
        self.assertEqual([f["name"] for f in overlay["crossNamespace"]["flows"]], ["a", "b", "sandbox-workers-to-mcp-gateway"])
        self.assertEqual(list(overlay), ["crossNamespace"])
        self.assertEqual(agi_packages.append_flow({}, "sandbox-mcp-gateway")["crossNamespace"]["flows"][0]["peer"]["namespace"],
                         "ewallet-sandbox")

    def test_append_flow_refuses_an_existing_name(self):
        flow = agi_packages.FLOWS["sandbox-mcp-gateway"]
        for existing in (dict(flow), {**flow, "port": 9}):
            with self.subTest(existing=existing.get("port")), self.assertRaises(ValueError):
                agi_packages.append_flow({"crossNamespace": {"flows": [existing]}}, "sandbox-mcp-gateway")

    def test_merge_follows_helm(self):
        merged = agi_packages.merge({"m": {"a": 1, "l": [1, 2]}, "k": 1}, {"m": {"b": 2, "l": [3]}})
        self.assertEqual(merged, {"m": {"a": 1, "b": 2, "l": [3]}, "k": 1})


if __name__ == "__main__":
    unittest.main()
