# I7 rubric crosswalk validation

The existing machine-readable crosswalk remains:

- `docs/rubric/rubric_evidence_matrix.csv`
- `docs/rubric/rubric_evidence_matrix.json`

Both files contain the same 93 rows spanning the authoritative workbook sheets
`edai-1 (50%)` and `edai-2 (50%)`. At I7 the recorded statuses are 12
`VERIFIED`, 72 `PARTIAL`, and 9 `OPEN`; the matrix reports 27/200 raw points and
a 13.5 weighted score. I7 does not change those scores or upgrade any status.

The matrix already records rubric sheet/row, requirement, source path/SHA,
tests, environment, evidence channels, metrics, verification status, missing
proof and next safe command. It is therefore the integrated crosswalk rather
than a second competing registry.

Limitations retained:

- Several source SHAs and evidence statements are point-in-time workstream
  records predating I6; `VERIFIED` is not automatically post-merge reproduced.
- Flink 500 eps semantic parity and the matched split-watermark performance
  rerun remain unresolved; the performance verdict is partial/mixed.
- DataHub retains seven historical PASS outcomes and one DP2 Gold
  `NOT_EXECUTED`; full GMS/UI gates are not inferred from source.
- RAG local SQLite evidence is not shared Redis/MCP business-E2E proof.
- Jenkins CPS/runtime, CD deployment, and live Observability runtime remain
  open.
- O1 checkpoint `9b475467e77121399bf6cae819fbbf768ba6b3c9` and active G2/G3 work are outside
  I7 and are not represented as integrated.

The original workbook remains the scoring authority. The crosswalk is evidence
traceability, not an award decision.
