# B1 Testing Quality Runbook

How to run the four B1 experiments and capture genuine evidence for EDAI-2 rows
28, 30, 31 and 32. Design and scope are in
[B1_TESTING_QUALITY.md](../../edai2/testing/B1_TESTING_QUALITY.md).

Everything runs locally. Nothing here contacts GKE or any cloud service, and the
current `kubectl` context does not matter.

Run the steps in order, from the repository root, on `feature/b1-testing-quality`.

## 1. Prerequisites

| Requirement | Check |
|---|---|
| Correct branch | `git branch --show-current` prints `feature/b1-testing-quality` |
| `uv` on `PATH` | `uv --version`; if missing: `export PATH="$HOME/miniconda3/envs/fintech/bin:$PATH"` |
| Free memory, at least 4 GiB | `free -g` |
| No heavy local workload | Check that no DP2 run or local vLLM pod is active; both compete for CPU and distort timing |
| Port 18080 free | `ss -ltn \| grep 18080` prints nothing |

Install and verify the locked environment:

```bash
uv sync --locked --project ai_platform/inference --group test --group load
uv run --locked --project ai_platform/inference python -m unittest discover -s tests/inference -t .
```

**Expected:** the last line of the second command is `OK`.

## 2. Coverage (row 28)

```bash
scripts/testing/run_coverage.sh
```

**Expected:** about 10 seconds, all tests pass, and the run ends with
`Required test coverage of 90.0% reached`.

| Artifact in `docs/evidence/edai2/testing/b1/coverage/` | Content |
|---|---|
| `terminal.txt` | Per-file table |
| `html/index.html` | Browsable report |
| `coverage.xml`, `coverage.json` | Machine-readable reports |
| `junit.xml` | Test results |
| `provenance.txt` | Commit, branch, time, lock hash |

**Screenshots:**

| File | What to capture |
|---|---|
| `coverage/screenshots/01_terminal_total.png` | Terminal with the per-file table and the total |
| `coverage/screenshots/02_html_index.png` | `html/index.html` in a browser, with the total visible |
| `coverage/screenshots/03_html_module_detail.png` | One module's page, for example `wire.py`, with highlighted lines |

**Interpretation:** row 28 needs a total above 90%. Compare the table with the
"Known uncovered paths" section of the design document. A new uncovered line
there means behaviour changed.

## 3. Property-based tests (row 31)

```bash
scripts/testing/run_property_tests.sh
HYPOTHESIS_PROFILE=b1-thorough HYPOTHESIS_SEED=20261106 scripts/testing/run_property_tests.sh
```

**Expected:** about 1 minute and 5 minutes. Both report passes and **13 xfailed**,
with no failures. The `xfailed` entries are the confirmed defects in
`test_known_defects.py`, not skipped work.

The B1-F6 end-to-end test needs the agent runtime, so it runs in that locked
environment, outside the repository:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/ewallet-b1-agent-venv PYTHONPATH=. \
  uv run --locked --project infra/docker/g3-byo-coordinator \
  python -m unittest tests.llm_agent.test_b1_f6_card_masking
```

**Expected:** `OK`, 5 tests.

| Artifact in `property/` | Content |
|---|---|
| `terminal-b1-ci.txt`, `terminal-b1-thorough.txt` | Results and Hypothesis statistics |
| `junit-b1-ci.xml`, `junit-b1-thorough.xml` | Test results |

**Screenshots:**

| File | What to capture |
|---|---|
| `property/screenshots/01_ci_profile_summary.png` | Final summary line of the `b1-ci` run |
| `property/screenshots/02_thorough_profile_summary.png` | Final summary line of the `b1-thorough` run |
| `property/screenshots/03_hypothesis_statistics.png` | Statistics of a property, showing the number of examples |

**Interpretation:** a real failure prints a minimal example and a
`@reproduce_failure` line. Keep the output: it is a finding, not noise. Any other
seed may find new counterexamples; that is the purpose of the profile.

## 4. Mutation testing (row 30)

```bash
B1_MUTATION_WORKDIR=/tmp/ewallet-b1-mutation B1_MUTATION_CHILDREN=6 scripts/testing/run_mutation.sh
```

**Expected:** about 7 to 10 minutes with 6 workers. The script ends by printing
the summary table and the workspace path. Lower `B1_MUTATION_CHILDREN` if the
laptop is needed for other work.

| Artifact in `mutation/` | Content |
|---|---|
| `run.txt` | mutmut progress output |
| `results.txt` | Every mutant and its status |
| `summary.md`, `summary.json` | Totals, both scores, per-module table |
| `mutmut-cicd-stats.json` | mutmut's own statistics |

Inspect a survivor, from the workspace:

```bash
cd /tmp/ewallet-b1-mutation
uv run --locked --project "$OLDPWD/ai_platform/inference" --group test mutmut show <mutant-name>
cd -
```

**Screenshots:**

| File | What to capture |
|---|---|
| `mutation/screenshots/01_mutmut_run_final.png` | Final progress line with the emoji counters |
| `mutation/screenshots/02_summary_table.png` | The summary table with both scores |
| `mutation/screenshots/03_survivor_example.png` | `mutmut show` for one survivor |

**Interpretation:** the target is an actionable score of at least 80%. Read
`Survived (actionable)` first. If a name appears under "Equivalence entries that
no longer match a survivor", the source changed since the review.

## 5. Load test (row 32)

```bash
LOCUST_USERS=20 LOCUST_SPAWN_RATE=5 LOCUST_RUN_TIME=2m scripts/testing/run_locust.sh
```

The script starts the mock-backed API on `127.0.0.1:18080`, waits for readiness,
runs Locust headless, writes the reports and stops the server, also on failure.

**Expected:** a little over 2 minutes, exit code 0, and a failure rate of 0%.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LOCUST_USERS` | 20 | Simulated users, at most 200 |
| `LOCUST_SPAWN_RATE` | 5 | Users started per second |
| `LOCUST_RUN_TIME` | 60s | Duration, at most 900 seconds |
| `LOCUST_MOCK_PORT` | 18080 | Local API port |
| `LOCUST_HOST` | `http://127.0.0.1:$LOCUST_MOCK_PORT` | Target |
| `LOCUST_MODEL_LATENCY_MS` | 5 | Simulated model delay |
| `EWALLET_LOAD_MIX` | `valid=60,invalid=10,concurrent=15,dependency_failure=10,health=5` | Scenario weights |
| `EWALLET_LOAD_SEED` | 20261106 | Request sequence seed |
| First argument | `docs/evidence/edai2/testing/b1/locust` | Output directory |

A higher-concurrency run for the concurrent scenario, kept apart from the main one:

```bash
LOCUST_USERS=100 LOCUST_SPAWN_RATE=20 LOCUST_RUN_TIME=2m \
EWALLET_LOAD_MIX="valid=50,concurrent=50" \
scripts/testing/run_locust.sh docs/evidence/edai2/testing/b1/locust/concurrent-100u
```

To explore interactively, start the server by hand and stop it with `Ctrl+C`:

```bash
uv run --locked --project ai_platform/inference --group load python -m tests.load.mock_server --port 18080
curl -s http://127.0.0.1:18080/health/ready
```

### Optional integration profile (disabled by default)

Only for a target you own and have reserved. It is not part of the B1 evidence,
and it sends real traffic to real dependencies.

```bash
EWALLET_LOAD_PROFILE=integration \
EWALLET_LOAD_ACKNOWLEDGE_REMOTE=I_OWN_THIS_TARGET \
LOCUST_HOST=https://<target-you-own> \
LOCUST_USERS=5 LOCUST_RUN_TIME=1m \
scripts/testing/run_locust.sh docs/evidence/edai2/testing/b1/locust/integration
```

Synthetic valid requests will mostly answer 424 against a real store because
their entities do not exist there, and Locust will count them as failures. Use
entities known to be materialized before reading anything into that run.

| Artifact in `locust/` | Content |
|---|---|
| `report.html` | Locust HTML report (the row 32 deliverable) |
| `locust_stats.csv`, `locust_stats_history.csv` | Per-endpoint and time-series data |
| `locust_failures.csv`, `locust_exceptions.csv` | Error breakdown |
| `summary.md`, `summary.json` | Total requests, throughput, p50/p95/p99, failure rate, errors |
| `run-parameters.txt`, `provenance.txt` | How and from which commit the run was made |
| `terminal.txt` | Locust summary output |

**Screenshots:**

| File | What to capture |
|---|---|
| `locust/screenshots/01_report_statistics.png` | `report.html`: request statistics table |
| `locust/screenshots/02_report_charts.png` | `report.html`: throughput, latency and user charts |
| `locust/screenshots/03_summary_md.png` | The rendered `summary.md`, with the mock-backed label visible |

**Interpretation:** the failure rate counts **unexpected outcomes**. HTTP 4xx and
5xx from the invalid and fault scenarios are expected and count as successes.
Always describe these results as mock-backed, never as production latency.

## 6. Evidence layout

```text
docs/evidence/edai2/testing/b1/
    coverage/   terminal.txt, html/, coverage.xml, coverage.json, junit.xml, provenance.txt, screenshots/
    property/   terminal-*.txt, junit-*.xml, provenance.txt, screenshots/
    mutation/   run.txt, results.txt, summary.md, summary.json, provenance.txt, screenshots/
    locust/     report.html, locust_*.csv, summary.md, summary.json, run-parameters.txt, provenance.txt, screenshots/
```

Capture real windows only. Keep the terminal prompt or the browser address bar in
view, and do not edit output. `.coverage` is ignored by Git and is not evidence.

After the runs, hand over the evidence folder. Phase B then updates `TESTING.md`,
`RUBRIC_EVIDENCE_INDEX.md`, `GAP_LEDGER.md` and `SCREENSHOT_AUDIT.md`.

## 7. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `uv is not on PATH` | Shell without the conda environment | Export the path from section 1 |
| `ModuleNotFoundError: hypothesis` | Group not installed | Rerun the `uv sync` from section 1 |
| `No solution found` or `--locked` fails | `pyproject.toml` and `uv.lock` differ | Run `git status`; do not relock unless a dependency changed on purpose |
| `refused: non-loopback targets...` | `LOCUST_HOST` is remote | Expected guard; see the integration profile |
| `refused: limits are...` | Above 200 users or 900 seconds | Reduce the values |
| `address already in use` | Port taken | Set `LOCUST_MOCK_PORT`, or see section 8 |
| Locust failure rate above 0% | Unexpected outcome | Read `summary.md`, then `locust_failures.csv` |
| Many `timeout` mutants | Machine overloaded | Lower `B1_MUTATION_CHILDREN` and stop other workloads |
| mutmut says `failed to collect stats` | A test fails before mutation | Run `tests/inference` and `tests/property`, and fix that first |
| Strict xfail reported as failed (`XPASS(strict)`) | A defect was fixed | Delete that test's marker and record the fix in `B1_FINDINGS.md` |
| `RuntimeError: greenlet is being finalized` at exit | Harmless gevent shutdown noise | Ignore |

## 8. Safe cleanup

```bash
# Temporary mutation workspace, only after you have finished with `mutmut show`.
rm -rf /tmp/ewallet-b1-mutation

# Hypothesis local cache, ignored by Git.
rm -rf .hypothesis

# A mock server left behind after a killed run. Check first, then stop that PID.
ss -ltnp | grep 18080
ps -eo pid,comm,args | awk '$2 ~ /^python/ && /mock_serve[r]/'
kill <pid>
```

To rerun a step, delete only that step's folder under
`docs/evidence/edai2/testing/b1/`.

Do not use `git clean`, `git reset` or `git stash`: the working tree contains
unrelated local changes that belong to other work.
