# Integrating B1, C-A and C-B

This page records how the three local branches depend on each other and the safest order to merge them. Nothing has been merged. All three branch from `origin/main` at `e00eab9`.

| Branch | Head | Files changed |
|---|---|---|
| `feature/b1-testing-quality` | `281db72` | 54 |
| `feature/c-data-platform-completion` | `7683511` | 55 |
| `feature/c-agent-platform-completion` | Latest local commit | About 110 |

## Dependencies

**No build or import dependency exists between C-A and C-B.** The only file all three change is `docs/runbooks/README.md`, and `git merge-tree` reports no conflict for any pair.

A simulated merge of C-A and C-B, exported without touching a branch, passed `tests/ci` and `tests/experiments`. An image built from it contains the watermark fix. `test_every_project_is_locked` fails there exactly as it does on `origin/main`, because its project list is stale; `uv lock --check` passes.

The report for Batch C-B said the streaming smoke depended on C-A. That's now tested and it does not:

| Image built from | Has the watermark fix | `cd_smoke.py --mode streaming` |
|---|---|---|
| C-B source only | No | PASS: 7 operators, 1 duplicate removed, 1 history record |
| C-A source | Yes | PASS: same result |

**The old smoke couldn't see the difference**, because the job plan is identical. C-A commit `c1f2d0e` changes `flink/job.py` so watermarks follow the payload's event time rather than the broker's record time.

**The release gate now can.** `cd_smoke.py` builds the image's real job and observes what reaches Flink: the source must receive no timestamp assigner, and one must be applied after it. Results at 512 MiB and 0.5 CPU, in about 18 seconds:

| Image | `event_time_after_source` | CD verdict |
|---|---|---|
| C-B source only | false | **FAIL** |
| C-A source | true | PASS |

So a streaming release from C-B alone now fails CD on purpose, until C-A is integrated.

The smoke also imports these modules from `data_platform/streaming`:

| Module | Changed by C-A |
|---|---|
| `config/settings.py` | Yes |
| `flink/job.py` | Yes, the watermark fix |
| `flink/processor.py` | No |
| `generator/scenarios.py` | No |

The image needs `data_platform/streaming`, `configs/streaming`, the Kafka connector JAR and `infra/docker/flink-benchmark`'s lockfile. All of these exist on both branches.

## Missing integration contracts

1. **Catalog ownership.** These C-A paths have no component in `jenkins/catalog/components.json`, so changing them triggers no CI: `infra/docker/streaming-sinks`, `infra/kubernetes/streaming-block-a`, `scripts/streaming`, `ops/mlops/feast_incremental.py`, `ops/schema`, `ops/gcp/spark_stage_report.py`, `ops/gcp/validate_gold_row_equivalence.py`, `scripts/mlops/feast_online_job.sh`.
2. **Lockfile registration.** `infra/docker/streaming-sinks` isn't in `python_projects`.
3. **Sink image release.** Block A's sink image has no release selector, so Jenkins can't build it.
4. **Row 36 scope.** CD proves the image and job plan. The running dual-store path is row 12's evidence, from C-A's `block_a_cloud.sh`.

Items 1–3 belong in the integration commit. They need both branches, and C-A must stay isolated until then.

## Recommended order

1. **B1 first.** It only adds tests and one masking fix, and the others don't depend on it.
2. **C-A next.** Data-layer changes come before the releases that package them.
3. **C-B last**, resolving `docs/runbooks/README.md`, which needs only both rows kept.
4. **Add the catalog entries** in one commit, then run the full test scope.
5. **Build release images from that merged commit.** Run P1 from there, never from C-B alone, so row 36 ships the watermark fix.

The owner has approved this order in principle; that isn't authorization to merge. Each merge remains the owner's decision. MCP and agent CD (P1) could run from C-B alone if needed, because they don't depend on C-A; only the streaming release should wait.
