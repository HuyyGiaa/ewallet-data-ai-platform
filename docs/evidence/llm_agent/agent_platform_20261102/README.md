# EDAI-2 Agent Platform evidence — 2026-11-02

This directory binds source/render/test evidence for P1–P4. It deliberately
separates historical G3 runtime evidence from current source validation.

- `git-state.txt`: isolated worktree and protected canonical readback.
- `gke-readback-blocker.txt`: current cluster metadata and exact API blocker.
- `rubric-gap-matrix.csv`: fail-closed requirement classification.
- `g3-autoscaling-render-summary.json`: deterministic Helm render assertions.
- `sandbox-approval-gate.json`: full pinned-chart cluster-scope/privilege audit.
- `historical-runtime-bindings.txt`: SHA-256 bindings to existing G3 evidence.
- `test-results.txt`: focused and combined local verification results.
- `SHA256SUMS`: integrity manifest for this directory (excluding itself).

No cloud mutation, Secret read, image build, node change or Helm upgrade was
performed. P2 runtime and P3/P4 runtime are not VERIFIED.

## Reproduce source validation

```bash
python3 -m unittest \
  tests.llm_agent.test_agent_platform_probe \
  tests.llm_agent.test_agents_chart \
  tests.unit.test_kagent_bounded_pilot

render_dir="$(mktemp -d /tmp/kagent-pilot-render.XXXXXX)"
infra/kubernetes/kagent-pilot/scripts/render.sh "${render_dir}"
python3 -m json.tool "${render_dir}/approval-gate.json"
```

The pilot render returning `BLOCKED_PENDING_OWNER_APPROVAL` is the expected
safety verdict. It is not permission to install Substrate.
