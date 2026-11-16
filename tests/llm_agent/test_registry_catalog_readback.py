"""registry_catalog readback: GET-only comparison of the registry with the deployed catalog (AGI-13)."""

from __future__ import annotations

import json

from ops.llm_agent.registry_catalog import readback

DOC = {"kind": "Agent", "metadata": {"name": "fraud-coordinator", "tag": "1.1.0",
                                     "labels": {"ewallet.io/runtime": "kagent-byo", "ewallet.io/git-sha": "new"}},
       "spec": {"title": "Coordinator", "description": "BYO"}}


def opener_for(stored, status=200):
    calls = []

    def opener(request):
        calls.append((request.get_method(), request.full_url))
        return status, json.dumps(stored).encode()
    return opener, calls


def test_a_matching_entry_passes_and_only_reads():
    stored = {"item": {"metadata": {"tag": "1.1.0", "labels": {"ewallet.io/runtime": "kagent-byo", "ewallet.io/git-sha": "old"}},
                       "spec": {"title": "Coordinator", "description": "BYO"}}}
    opener, calls = opener_for(stored)
    report = readback([DOC], "http://127.0.0.1:18121", opener=opener)
    assert report["verdict"] == "PASS" and report["entries"][0]["published_git_sha"] == "old"
    assert calls == [("GET", "http://127.0.0.1:18121/v0/agents/fraud-coordinator/1.1.0")]


def test_a_missing_or_stale_entry_fails_with_the_difference():
    opener, _ = opener_for({"title": "Not Found"}, status=404)
    missing = readback([DOC], "http://127.0.0.1:18121", opener=opener)["entries"][0]
    assert (missing["registered"], missing["matches"]) == (False, False)
    stale = {"metadata": {"tag": "1.1.0", "labels": {"ewallet.io/runtime": "kagent-sandboxagent"}},
             "spec": {"title": "Coordinator", "description": "SandboxAgent"}}
    opener, _ = opener_for(stale)
    entry = readback([DOC], "http://127.0.0.1:18121", opener=opener)["entries"][0]
    assert entry["registered"] and not entry["matches"]
    assert any("ewallet.io/runtime" in item for item in entry["differences"]) and "spec.description differs" in entry["differences"]


def test_p4_specialists_are_catalogued_as_sandbox_agents_with_their_image():
    from pathlib import Path
    import yaml
    from ops.llm_agent.registry_catalog import CHART, build_catalog, merge_values

    values = yaml.safe_load((CHART / "values.yaml").read_text())
    for overlay in ("g3/g3.yaml", "agi/agents.yaml"):
        values = merge_values(values, yaml.safe_load((CHART / overlay).read_text()) or {})
    image = "asia-southeast1-docker.pkg.dev/p/r/i@sha256:" + "ab" * 32
    values = merge_values(values, {"agents": {
        "coordinator": {"byo": {"sandboxSpecialists": ["drift-specialist", "feature-rag-specialist"]}},
        "specialists": {"drift-specialist": {"byo": {"image": image}}}}})
    fleet = "asia-southeast1-docker.pkg.dev/p/r/fleet@sha256:" + "cd" * 32
    agents = {d["metadata"]["name"]: d for d in build_catalog(values["agents"], CHART / "prompts", "0" * 40,
                                                              sandbox_image=fleet) if d["kind"] == "Agent"}
    assert agents["drift-specialist"]["metadata"]["labels"]["ewallet.io/runtime"] == "kagent-sandboxagent-byo"
    assert agents["feature-rag-specialist"]["metadata"]["labels"]["ewallet.io/runtime"] == "kagent-sandboxagent-byo"
    assert agents["fraud-coordinator"]["metadata"]["labels"]["ewallet.io/runtime"] == "kagent-byo"
    assert agents["drift-specialist"]["metadata"]["labels"]["ewallet.io/byo-image-digest"] == "sha256-" + "ab" * 8
    assert agents["drift-specialist"]["metadata"]["labels"]["ewallet.io/sandbox-image-digest"] == "sha256-" + "cd" * 8
    assert "ewallet.io/sandbox-image-digest" not in agents["fraud-coordinator"]["metadata"]["labels"]
    assert "SandboxAgent" in agents["drift-specialist"]["spec"]["description"]
