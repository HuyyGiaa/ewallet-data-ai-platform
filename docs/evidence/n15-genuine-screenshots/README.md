# N15 genuine screenshot provenance

N15 integrates original owner-captured Ubuntu screenshots without cropping, editing or re-rendering. Every repository copy is byte-identical to its source under `/home/huyy-giaa/Pictures/Screenshots/`; hashes are in [SHA256SUMS](SHA256SUMS).

| Repository image | Original local source | Classification | Rubric rows | Proof boundary |
|---|---|---|---|---|
| `../edai1/docker/03_docker_image_ab_historical_terminal.png` | `01_docker_image_ab.png` | `GENUINE_HISTORICAL_EVIDENCE_VIEW` | EDAI-1 3–4 | Displays preserved A/B JSON; no N15 rebuild |
| `../edai1/docker/04_docker_compose_historical_terminal.png` | `02_docker_compose_genuine_terminal.png` | `GENUINE_HISTORICAL_EVIDENCE_VIEW` | EDAI-1 3–4 | Displays preserved Compose output; no N15 rerun |
| `../edai1/storage/08_postgres_warehouse_index_ab_historical_terminal.png` | `07_postgres_warehouse_index_ab_genuine_terminal.png` | `GENUINE_HISTORICAL_EVIDENCE_VIEW` | EDAI-1 21 | Displays preserved 2M-row result; no N15 benchmark |
| `../edai2/screenshots/runtime/11_agents_hpa_pdb_live_runtime.png` | `Screenshot From 2026-11-06 00-11-49.png` | `GENUINE_LIVE_RUNTIME` | EDAI-2 16,19,22,25 | Deployments/HPA/PDB live; no scale event or KEDA |
| `../edai2/screenshots/runtime/04_mcp_rollingupdate_live_runtime.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 18 | Ready + RollingUpdate configured; no rollout/fallback test |
| `../edai2/screenshots/runtime/03_sandbox_substrate_live_runtime.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 20,27 | Pilot sandbox only; not business agents |
| `../edai2/screenshots/runtime/05_kserve_inferenceservice_live.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 15 | InferenceService and predictor ready |
| `../edai2/screenshots/runtime/06_kserve_v2_metadata_live.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 15 | V2 ready 200 and 41-input metadata |
| `../edai2/screenshots/runtime/07_fraud_api_prediction_kserve_correlated_live.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 15 | Prediction 200 + matching KServe ID; no parity proof |
| `../edai2/screenshots/runtime/08a_fraud_api_health_live.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 16 | Live/ready 200; parity flags false |
| `../edai2/screenshots/runtime/08b_fraud_api_validation_422_part1.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 16–17 | HTTP 422, first half |
| `../edai2/screenshots/runtime/08c_fraud_api_validation_422_part2.png` | same basename | `GENUINE_LIVE_RUNTIME` | EDAI-2 16–17 | Field errors, continuation |

## Rejected candidates

The `23-54-43` and `23-55-06` captures show failed cluster access. `23-55-11` and `23-57-17` show a real terminal viewing the historical N13 transcript, not a live query. `00-00-03` and `00-02-03` are connectivity diagnostics. `00-19-41` duplicates the cleaner named MCP capture. None is submitted as new evidence.

N15 created no synthetic image, overwrote no N14 PNG and preserved all raw JSON/log links.
