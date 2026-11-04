# Flink screenshot evidence manifest — 2026-11-01

This manifest describes the original, byte-for-byte screenshots imported from
`submission/flink-benchmark-20261101-v2@733a82a2e55769f936335b0f6b457bc19c9d0a1b`.
The filenames and image bytes are intentionally unchanged. Timestamps below are
the filename/UI timestamps; the exact benchmark namespace is not visible in the
screenshots and remains `UNKNOWN`.

The screenshots show two 100 eps jobs built from Git
`22c0c7f6ad501bf37f19053d648e5dc173a0753d` with identical logical and transport
input digests. They do not resolve the separately recorded 500 eps semantic
parity failure, and they do not prove GCP, Feast Online, or production recovery.

| Original path | Git blob | Bytes | SHA-256 | Profile / identity | What the screenshot demonstrates |
|---|---|---:|---|---|---|
| `docs/images/Screenshot From 2026-11-01 04-11-08.png` | `e6badeaab9a509fabed5efc02bf1b4a05d3dfd0d` | 239749 | `d8cf7b6dc55b50c7dcc7571fcb73e530187aa0769f133075a3a81821c56ad3ec` | baseline; Flink job `38c6d2f9406b01de21fd3f11a60817d6` | Flink 1.19.1 job is RUNNING with the expected source, dedup, event-time, 31-feature-state and sink topology at parallelism 1. |
| `docs/images/Screenshot From 2026-11-01 04-11-56.png` | `e1243a51552d5570a74a018103210cfab0cfa9fa` | 135841 | `55d192cbd3bf66947b39f9d671128a86385ac2b888eeca1f3ce993c5d01a1798` | baseline; same job | TaskManager resource view: 4 slots, 7.00 GB physical memory, 2.45 GB JVM heap and 2.06 GB managed memory at capture time. |
| `docs/images/Screenshot From 2026-11-01 04-12-16.png` | `edd565c78a735dd234d4a07ce13c8e6975678b7e` | 173001 | `3ca1c6d2b35bc2e9bce739b7f147bbacff0852e7e19be8f986144eac605bc53d` | baseline; same job | Baseline feature-state operator shows HIGH backpressure (73%) during the captured interval; this is a point-in-time UI observation, not a run aggregate. |
| `docs/images/Screenshot From 2026-11-01 04-12-23.png` | `17432e2b66e8d08387f3133be16d74223f0a504e` | 169925 | `b7ec00e62aae65c26b488e115dda9fdbdc8615a6999279ba7a568760b18bfe6f` | baseline; same job | Baseline watermark UI shows the active subtask watermark at capture time. |
| `docs/images/Screenshot From 2026-11-01 04-15-53.png` | `b9d1bd689a5e822c1dae6b0a532c8e4f5162f0bd` | 233633 | `3295878eba415fa3fd1d59110bf5e0f895ca9bc756b6ec097d5883d4a1f47425` | optimized; Flink job `fb94d527363aa8c0677a02c5834aa34b` | The same logical topology is RUNNING at parallelism 4. |
| `docs/images/Screenshot From 2026-11-01 04-16-46.png` | `5868535928131ae5d08a93bf0e63ab95621ccee3` | 179549 | `efb5910fc90a1f5e96b8f460e353c3b580ed81c72b939c63c9650f6bc3e44796` | optimized; same job | Four feature-state subtasks report 0–3% backpressure and OK status at capture time. |
| `docs/images/Screenshot From 2026-11-01 04-17-00.png` | `d3c9afc8a6f3c9da77b47027810872911c6c7a8e` | 179724 | `088470a565740685cf27766b6616f5b6117ef15272b476e8950d595141311e18` | optimized; same job | Four per-subtask watermarks are equal at capture time, evidencing split-level watermark alignment for this 100 eps run. |
| `docs/images/Screenshot From 2026-11-01 04-17-12.png` | `3a474fff71fb9402c0d127b53ef82238a5aa7c35` | 136223 | `cfb368730288d52921d677337eb97b8026bb79b0c73284b35308d5b1800f6767` | optimized; same job | TaskManager resource view: all 4 slots used, 7.00 GB physical memory and 2.06 GB managed memory fully allocated at capture time. |
| `docs/images/Screenshot From 2026-11-01 04-25-06.png` | `bc902acc7edfc3b909d6aafa48633f29611e4bfb` | 112868 | `6930d365c6a35b6483b36ccd7c4591686b414734f65fb441e3d01693a849ee95` | baseline vs optimized; target/achieved 100 eps | Terminal comparison records identical input digests and 12,000 accepted / 35,975 online updates for both profiles, with `functional_checks_passed=True`; optimized has lower shown latency. It also records one failed checkpoint in each run, so this image is not standalone proof of production readiness. |

## Related evidence and limitations

- `docs/streaming/flink_local_benchmark_results_20261101.md` is the tracked
  comparison report and remains authoritative for the PARTIAL/MIXED verdict.
- The raw `run.json`, `outputs_summary.json`, `flink_metrics.json`,
  `docker_stats.jsonl`, logs, and compare outputs are referenced under the
  git-ignored `evidence/flink-benchmark/<namespace>/` directories on the
  benchmark PC. They are not present in this Git branch and are therefore not
  claimed as integrated artifacts.
- The report records valid semantic parity at 100 eps and a semantic parity
  failure at 500 eps. The later split-watermark source correction still needs a
  matched baseline/optimized 500 eps rerun before performance or optimization
  can be marked verified at that load.
