# Owner single-run benchmark campaign (2026-11-09/10)

The official EDAI-2 LLM benchmark evidence: one baseline and one optimized run per experiment, run on GKE by the owner, with screenshots captured by hand during the sessions.

Results and analysis: [BENCHMARK_RESULTS.md](../../../../edai2/llm/BENCHMARK_RESULTS.md). Claim-by-claim mapping: [BENCHMARK_EVIDENCE_MATRIX.md](../../../../edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md).

| Experiment | Session (UTC) | Source commit | Execution validity | Observed outcome |
|---|---|---|---|---|
| EXP-V1, thread binding | 2026-11-09 17:45–19:20 | `263c1d1f` | VALID | `OBSERVED_IMPROVEMENT` |
| EXP-V2, Service vs llm-d EPP | 2026-11-10 07:43–09:22 | `263c1d1f` | VALID | `OBSERVED_MIXED` |

Both sessions used `EXP_PROFILE=single`, ran in the temporary namespace `ewallet-bench`, ended with `session end status=0`, and left production unchanged. One baseline request failed in EXP-V1 and is recorded as a deviation. This is **not** the multi-run [rehearsal](../benchmark_rehearsal_20261109/README.md).

| Path | Content |
|---|---|
| `exp-v1/`, `exp-v2/` | `manifest.json`, `session.log`, `setup.yaml`, per-arm `config/`, `raw/`, `metrics/`, `logs/`, `screenshots/`; `comparison/`; `gateway-policy/`; `observability/`; `safety/`; `production-*.txt` |
| `exp-v1.terminal.log`, `exp-v2.terminal.log` | the runner's terminal output and `runner exit` line |
| `screenshots/` | cleanup screenshots of both sessions |
| [`screenshot_provenance.json`](screenshot_provenance.json) | for each of the 35 screenshots: source name, destination, SHA-256, experiment, arm, evidence IDs, run, description, limitation |
| `sha256sums.txt` | checksums of every file here |

**Screenshots** are genuine captures, byte-identical to the owner's originals, with no cropping or editing. Evidence IDs that share a screen map to one file. `POL-4b_later_burst_503.png` is the owner's capture `Screenshot From 2026-11-10 15-30-18.png`, renamed only.
