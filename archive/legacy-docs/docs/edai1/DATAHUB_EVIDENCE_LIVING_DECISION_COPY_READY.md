# Copy-ready Living Decision — DataHub evidence organization

Latest canonical heading read on 01/11/2026 ICT was `LD21.316 ADDENDUM 12`;
`21.317` remains reserved for the G1 runtime milestone. This entry therefore
continues the current decision as an addendum and does not consume 21.317.

## LD21.316 ADDENDUM 13 — 01/11/2026 ICT — DATAHUB EVIDENCE ORGANIZED AND SOURCE HANDOFF READY; POST-MERGE REPRODUCTION OPEN

SCOPE AND PROVENANCE: In isolated worktree
`.worktrees/codex-datahub-quality-assertions`, branch
`codex/datahub-quality-assertions`, the three previously tested runtime fixes
were reviewed, tested and committed as `5926a7af608523339d4a853291646faeda4168c7`
(`fix(datahub): finalize assertion publication and readback`). No canonical
checkout, DP1/DP2/DP3 pipeline, cloud resource, G1/RAG/Flink/ML worktree or
immutable data was changed.

EVIDENCE ORGANIZATION: Ten existing screenshots were copied byte-for-byte into
`docs/evidence/images/datahub/`: five lineage/DataFlow, four assertions and one
schema. Originals remain in place. A CSV manifest records absolute source,
SHA-256, run identity, historical/current classification, rubric mapping and
the narrow claim each image supports. Image hashes match their originals, all
Markdown image links resolve and no exact duplicate was committed under two
names. No visible credentials or tokens were observed.

TRUTH BOUNDARY: Real GMS publication/readback remains 8 assertion definitions
and 7 evidence-backed historical PASS events. DP2 Gold remains definition-only
and NOT_EXECUTED. Only DP1 has a new result-bound assertion UI screenshot; DP2
Silver, DP2 Gold zero-run and four DP3 PIT UI screenshots remain missing. Three
older Feature assertion screens prove only Cloud Delta Metadata Contract checks,
not PIT validation. SCD2 row35 is missing; feature row36 is partial because
`event_timestamp` is visible but `created` is not.

VERIFICATION AND HANDOFF: DataHub uv lock check passed; focused tests reported
24 passed/12 optional Feast skipped/one SDK experimental warning; diff checks
passed. Documentation commit is the commit containing this entry; record its
exact SHA from the final Git handoff before pasting into Google Docs. Branch is
ready for review only; no merge or push occurred. POST_MERGE_REPRODUCED remains NO until
the owner integrates the reviewed commits into official main and reruns the
locked publication/readback procedure from that exact merged-main SHA.

Google Docs was read successfully for numbering, but this copy-ready text was
not written back. Do not claim the Living Decisions document was updated.
