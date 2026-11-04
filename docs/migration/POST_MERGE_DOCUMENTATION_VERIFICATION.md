# Post-merge documentation verification runbook

Status: prepared only. Do not claim `POST_MERGE_REPRODUCED` until these checks
run from the actual merged `main` SHA.

1. Check out a clean worktree at the exact merged `main` commit and record the
   full SHA.
2. Open `docs/README.md`, then follow the EDAI-1, EDAI-2, runbook, evidence, and
   rubric links.
3. Run the Phase A link check documented in
   `docs/migration/phase_a/DOCUMENTATION_LINK_VALIDATION.md`.
4. Confirm every `docs/DOCUMENTATION_REGISTRY.csv` path exists and every status
   uses the approved vocabulary.
5. Verify the portable source inventory, rebuild the mapping, and compare both
   generated outputs:

   ```bash
   (cd docs/migration/evidence && sha256sum -c EVIDENCE_INVENTORY_SNAPSHOT.sha256)
   python3 docs/migration/evidence/build_evidence_migration_map.py \
     --inventory docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.csv \
     --repo-root . \
     --map-output /tmp/EVIDENCE_MIGRATION_MAP_V2.csv \
     --validation-output /tmp/EVIDENCE_MAPPING_VALIDATION.json
   cmp docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv \
     /tmp/EVIDENCE_MIGRATION_MAP_V2.csv
   cmp docs/migration/evidence/EVIDENCE_MAPPING_VALIDATION.json \
     /tmp/EVIDENCE_MAPPING_VALIDATION.json
   python3 docs/migration/evidence/validate_evidence_migration_map.py \
     docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv \
     --expected-rows 1445
   ```

6. Confirm the validator reports zero duplicate destinations, zero errors, and
   `artifacts_migrated=0` for this B0 snapshot.
7. For any external artifact selected in a later phase, verify its real durable
   URI, object version, checksum, and retrieval instructions. The B0 placeholder
   is not sufficient.
8. Reproduce one owner-approved evidence item per component from its documented
   runbook without relying on another worktree's untracked files.
9. Record the merged SHA, commands, results, and any drift in a new evidence
   manifest. Do not overwrite the Phase B0 map.

Only after all applicable checks pass may the milestone be labeled
`POST_MERGE_REPRODUCED`.
