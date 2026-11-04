# I7 controlled documentation and evidence integration report

## Scope and strategy

I7 began from I6 `4801300ec47d59d0e6b854b5b82b3cb9e4a474b3` and used the
Phase A+B0 source `4bb04a1942a84cf7745e8b8e6ba261a35a1d30fd`. Their merge base
is `8ed5b94ef75a818d69d13085d68c9703a785d57c` (104 target-only and
three source-only commits).

A full merge was rejected: the historical documentation branch does not contain
I0-I6 platform source and would reintroduce obsolete shared files or delete
authoritative integrated paths. I7 therefore selectively imported the Phase
A/B0 documentation and mapping artifacts, then semantically merged only the
shared documentation indexes. No business source, dependency lock, CI mapping,
evidence payload, canonical branch, main branch, cloud resource or Google Doc
was modified.

## Registry and links

- Historical snapshot: 99 rows, SHA-256
  `7039d634dff1947fca64c634007cb4205e90fbc7384282416849105941a4d28c`.
- I7 registry: 137 unique existing paths; 38 later I2-I6 documents remain
  `UNKNOWN` pending row-level review.
- Statuses: `CURRENT=5`, `HISTORICAL=30`, `PARTIAL=35`, `PROPOSED=1`,
  `UNKNOWN=66`.
- Links: 305 checked, 296 valid relative, nine external, zero missing, zero
  unsafe escapes.

## Phase B0 mapping

- Portable inventory: 1,445 rows, SHA-256
  `2f7f9323f3655832c9612c02751289c8c7609e5eeaf22fed4a616fe30d9ed3b1`.
- Mapping SHA-256:
  `71660d1796b3e66dd22494b94e00303b9ad59ab9a7ba258cc62742d3f38905d0`.
- Validation SHA-256:
  `db55030c37eba27a06377b9d8efb9b441e73ce24dfbb3b8c4cc004bd79610aab`.
- Deterministic rebuild: byte-for-byte PASS for CSV and JSON.
- Dispositions: 862 `PLANNED`, 164 `BLOCKED_FOR_REVIEW`, 81
  `KEEP_IN_PLACE`, 134 `EXTERNAL_REFERENCE`, 204 `NOT_EVIDENCE`.
- Hashes: 1,047 verified and 398 unknown.
- Manual review: 245; duplicate corrected destinations: zero; migrated: zero.

## Evidence and rubric integrity

All 414 files tracked under `docs/evidence/` at I6 were compared by SHA-256
against the I6 Git blobs: 414/414 byte-identical. I7 adds only the navigation
README under this directory, not a raw artifact. That tree contains 28 images,
including two earlier Flink images and the ten original DataHub images under
`docs/evidence/images/datahub/`. The nine original Flink V2 screenshots are
stored separately under `docs/images/`; all 9/9 also match their I6 Git blobs.
The portable map retains external/worktree references without copying them.

The existing rubric CSV/JSON contain the same 93 rows and status summary (12
verified, 72 partial, nine open). I7 does not change official requirements,
points or runtime claims.

## CI shared-path result

The focused change-detection probe selected only `observability` for
`ai_platform/llm_agent/tools/service.py`; an adjacent tool path selects
`mcp-agents`. The 21 existing CI catalog/change-detection tests pass, but they
do not enforce dual-owner fanout for the shared service file. This is an open,
separately authorized CI correction, not an I7 source change.

## Security and limitations

Focused scans found no private keys, provider tokens, hard-coded credential
values or customer payloads in the integrated documentation/mapping files.
Historical reports contain local worktree paths as provenance; they are not
credentials and are not runtime dependencies. Fifteen potentially sensitive
inventory paths remain opaque as designed.

I7 does not establish post-merge runtime verification, does not integrate O1 or
active G2/G3, and does not authorize Phase B1.
