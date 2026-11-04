# Copy-ready Living Decision draft for I7

Read-only Google Docs export on 01/11/2026 ICT found `LD21.324` as the latest
entry. Recheck immediately before copying; if a newer entry exists, increment
the identifier and preserve chronological order. Do not overwrite historical
Addendum 16.

## LD21.325 — 01/11/2026 ICT — I7 DOCUMENTATION/PHASE B0 REGISTRY INTEGRATED; EVIDENCE MIGRATION REMAINS PROHIBITED

PROVENANCE: Controlled I7 integration started from
`4801300ec47d59d0e6b854b5b82b3cb9e4a474b3` (tree
`4c18beeb51af0ba7cb7871a5567847383f0a8573`) on
`integration/final-integration-20261101`. The authoritative Phase A+B0 source
was `4bb04a1942a84cf7745e8b8e6ba261a35a1d30fd`, merge base
`8ed5b94ef75a818d69d13085d68c9703a785d57c`. Because that historical branch is
based on stale platform source, I7 selectively integrated only documentation,
navigation and Phase B0 planning artifacts; it did not merge or replace
production source.

DOCUMENTATION: The accepted 99-row Phase A registry is preserved as an
immutable snapshot. A deterministic I7 rebuild adds 38 documents introduced by
I2-I6 with status `UNKNOWN`, producing 137 unique existing paths:
`CURRENT=5`, `HISTORICAL=30`, `PARTIAL=35`, `PROPOSED=1`, `UNKNOWN=66`.
Markdown validation checked 305 links: 296 valid repository-relative links and
9 external links, with zero missing navigation links and zero repository
escapes.

EVIDENCE/MAPPING: The portable inventory checksum remains
`2f7f9323f3655832c9612c02751289c8c7609e5eeaf22fed4a616fe30d9ed3b1`.
Clean rebuild reproduced the 1,445-row map and validation JSON byte-for-byte.
Results remain 164 `BLOCKED_FOR_REVIEW`, 81 `KEEP_IN_PLACE`, 398
`UNKNOWN_HASH`, 134 external references, 245 manual-review items, zero unsafe
destination collisions, and `artifacts_migrated=0`. All 414 files under the I6
`docs/evidence/` tree and all nine original Flink V2 screenshots under
`docs/images/` were byte-identical after integration. Phase B1 copy/move/
rename/delete remains unauthorized.

RUBRIC/TRUTH: The existing 93-row EDAI crosswalk remains 12 `VERIFIED`, 72
`PARTIAL`, 9 `OPEN`; no rubric score or runtime status was upgraded. O1/G2/G3
remain outside I7. Historical runtime evidence is not post-merge reproduction.

OPEN FOLLOW-UP: CI change detection for
`ai_platform/llm_agent/tools/service.py` selects Observability only and does not
fan out to MCP/security tests. I7 records this shared-path gap without modifying
the locked 14-component CI catalog. A separately authorized CI correction is
required. Next integration work must begin with bounded O1 preflight and a fresh
Claude G2/G3 ownership handoff; no automatic cloud or source integration is
authorized by this decision.
