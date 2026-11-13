# EDAI-1 Docker A/B evidence (5GB campaign)

Owner run `20261112t142522` of `python3 -m ops.docker_ab` (tool commit `e3874443`) and the hardened Compose run, analysed in [DOCKER_OPTIMIZATION.md](../../../../edai1/docker/DOCKER_OPTIMIZATION.md).

Every screenshot is an owner capture copied byte-for-byte from `~/Pictures/Screenshots/`. The SHA-256 of each original was checked before and after the copy and equals the committed file. Images were not edited. `SHA256SUMS` covers every screenshot and raw file; check it with `sha256sum -c SHA256SUMS` from this folder. `MANIFEST.json` holds the same inventory in machine-readable form.

Large data stays out of Git: the 351 MB warehouse Parquet snapshot, the Delta tables and the Spark event logs remain in `~/ewallet-evidence/` and the benchmark bucket. The PostgreSQL password file was never copied.

## Screenshots

| Checkpoint | File | Original file name | What is visible | SHA-256 (first 16) |
|---|---|---|---|---|
| DK01 | [DK01_dockerfiles_baseline_vs_optimized.png](screenshots/DK01_dockerfiles_baseline_vs_optimized.png) | `DK01_dockerfile_before_after.png` | Editor: Dockerfile.baseline vs Dockerfile.optimized | `d3162a29fc3a9978` |
| DK02 | [DK02a_build_baseline.png](screenshots/DK02a_build_baseline.png) | `DK02a_baseline_build.png` | Terminal: baseline build log | `89e425d9b2112fae` |
| DK02 | [DK02b_build_optimized.png](screenshots/DK02b_build_optimized.png) | `DK02b_optimized_build.png` | Terminal: optimized build log | `7c2bdcd09f643709` |
| DK02 | [DK02c_build_logs_tail_exit_code_0.png](screenshots/DK02c_build_logs_tail_exit_code_0.png) | `Screenshot From 2026-11-12 21-29-12.png` | Terminal: tail of build_baseline.log and build_optimized.log, both [exit code 0] | `86599d2507e9866b` |
| DK03 | [DK03_image_size_comparison.png](screenshots/DK03_image_size_comparison.png) | `DK03_docker_image_size_comparison.png` | Terminal: summary.txt size table and attribution | `1f4d716eca9feda8` |
| DK04 | [DK04a_failed_attempt_spec_unset_invalid_spec.png](screenshots/DK04a_failed_attempt_spec_unset_invalid_spec.png) | `Screenshot From 2026-11-12 21-35-43.png` | Terminal: summary.txt, then a DK04 attempt with $SPEC unset -> INVALID_SPEC (kept as a genuine failed attempt) | `85f5a931def5166c` |
| DK04 | [DK04b_functional_equivalence_pass.png](screenshots/DK04b_functional_equivalence_pass.png) | `DK04_functional_equivalence.png` | Terminal: baseline and optimized print the same report_sha256 and PASS | `27aca6846fdc3cb3` |
| DK04/DK05 | [DK04c_rerun_pass_and_compose_config.png](screenshots/DK04c_rerun_pass_and_compose_config.png) | `Screenshot From 2026-11-12 21-38-25.png` | VS Code terminal: DK04 rerun PASS and docker compose config (hardening) | `6d120c9ccfd5cf40` |
| DK05 | [DK05a_compose_config_and_run_exit_0.png](screenshots/DK05a_compose_config_and_run_exit_0.png) | `Screenshot From 2026-11-12 21-49-16.png` | VS Code terminal: compose config, run --rm campaign-verifier PASS, exit=0 | `5b3acd4e5f41bb04` |
| DK05 | [DK05b_compose_runtime_no_residual_containers.png](screenshots/DK05b_compose_runtime_no_residual_containers.png) | `DK05b_compose_runtime.png` | Terminal: compose runtime and empty ps -a | `ff1126f920241b1e` |

## Raw artifacts

| File | Bytes | SHA-256 (first 16) |
|---|---:|---|
| [raw/build_baseline-slim.log](raw/build_baseline-slim.log) | 2,455 | `88e29f3ffb93c92f` |
| [raw/build_baseline.log](raw/build_baseline.log) | 2,808 | `45212efbb117c70d` |
| [raw/build_optimized-full.log](raw/build_optimized-full.log) | 3,456 | `a83c856fee817588` |
| [raw/build_optimized.log](raw/build_optimized.log) | 3,340 | `892e12f8e8530aa8` |
| [raw/compose_build.txt](raw/compose_build.txt) | 2,845 | `7f6b7a7caa6aa47a` |
| [raw/compose_config.txt](raw/compose_config.txt) | 1,205 | `0f638680a88deeab` |
| [raw/compose_run.txt](raw/compose_run.txt) | 391 | `997e2485558ad25c` |
| [raw/docker_stdout.txt](raw/docker_stdout.txt) | 2,052 | `9d1901d12d0367d8` |
| [raw/result.json](raw/result.json) | 38,832 | `9522b091adc25a1a` |
| [raw/summary.txt](raw/summary.txt) | 2,052 | `9d1901d12d0367d8` |

## Screenshots reviewed and not included

| Original file name | Reason |
|---|---|
| `Screenshot From 2026-11-12 21-10-12.png` | Shows the Claude Code panel and a different directive; Docker disk preflight only |
| `Screenshot From 2026-11-12 21-12-27.png` | Preflight: ADC check and a home-cache listing; no Docker result |
| `Screenshot From 2026-11-12 21-50-08.png` | Duplicate of DK05a plus an inspection of the earlier warehouse volume |
