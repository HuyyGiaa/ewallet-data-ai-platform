# Living Decisions entry - copy-ready draft

Authenticated Google Docs editing was not available to this implementation
session. The text below must be appended and read back by the owner or an
authenticated operator. Do not assign a Living Decision number until the
canonical document supplies the next number.

## [NEXT LD NUMBER] - Phase A documentation navigation and Phase B0 collision-safe evidence mapping

Date: 2026-11-01 ICT.

Repository maintenance was implemented in isolated worktree
`/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/docs-evidence-cleanup`
on branch `codex/docs-evidence-phase-a-b0`, based on canonical
`feature/phase2-fraud-platform@8ed5b94ef75a818d69d13085d68c9703a785d57c`.

Phase A added central documentation navigation for EDAI-1, EDAI-2, runbooks and
evidence, plus a 99-row documentation registry using CURRENT, HISTORICAL,
PARTIAL, PROPOSED and UNKNOWN. Link validation checked 136 repository-relative
links across root and new indexes with zero missing/escaping targets. No source,
runtime configuration, runbook command or tracked evidence content changed.

Phase B0 processed all 1,445 audit evidence rows. The original map contained 238
colliding destination groups affecting 762 rows: 116 same-content groups/260
rows, 85 different-content groups/327 rows, and 37 unknown-hash groups/175 rows.
The deterministic V2 map has zero destination collisions. It records 862
PLANNED, 81 KEEP_IN_PLACE, 134 EXTERNAL_REFERENCE, 164 BLOCKED_FOR_REVIEW and
204 NOT_EVIDENCE rows. It reports 398 artifacts without a verified SHA-256, 204
source/build artifacts excluded from evidence, and 245 owner-review items.

No evidence file was copied, moved, renamed, deleted or uploaded. No active G1,
RAG, DataHub, Streaming, MLOps, DP2 or DP3 worktree was modified. DataHub's
superseded `docs/evidence/images/datahub/` path is mapped to the approved
`docs/evidence/edai1/datahub/` taxonomy but remains untouched pending owner
coordination. Large artifacts remain external-reference plans; no cloud storage
was provisioned.

Validation: `git diff --check` PASS; documentation registry validation PASS;
Markdown link validation PASS; evidence mapping validator PASS with 1,445 unique
sources, 1,445 unique artifact IDs, zero duplicate destinations and zero errors.
Phase B0 is review-ready but Phase B1 migration remains blocked on explicit owner
approval, active-worktree checkpoints, durable external URI policy, and manual
review of 245 rows. Branch commits and final feature HEAD must be copied from the
implementation report after commit creation and independently verified before
integration. No merge or push was performed.

## [NEXT LD NUMBER] - Phase A/B0 review closure: portable inventory and count reconciliation

Date: 2026-11-01 ICT.

The Phase B0 mapping input is now a sanitized, metadata-only, versioned snapshot
at `docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.csv`, protected by its
committed SHA-256 file. A clean repository checkout can deterministically
rebuild and byte-compare both the 1,445-row migration map and validation JSON
without the original local audit directory. Machine-specific home prefixes and
15 potentially sensitive path names were sanitized; stable pre-sanitization
source identities preserve artifact IDs and the zero-destination-collision
result. No evidence payload was committed or moved.

The prior 107-file audit count and 99-row registry reconcile as 107 minus 11
local-only, untracked files plus 3 new Phase A navigation indexes. The 11
excluded paths are absent from clean checkout (nine are also ignored); their
exclusion does not authorize deletion or evidence movement. Registry status
counts remain CURRENT=5, HISTORICAL=30, PARTIAL=35, PROPOSED=1, UNKNOWN=28.
Phase B1 remains out of scope. Record the focused correction commit SHA and
merged canonical SHA when integration is approved; no merge or push was
performed by this change.
