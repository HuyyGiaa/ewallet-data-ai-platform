# Portable evidence inventory snapshot

`EVIDENCE_INVENTORY_SNAPSHOT.csv` is the versioned metadata input for rebuilding
the Phase B0 evidence migration map from a clean checkout. It contains 1,445
rows and no evidence payloads.

The snapshot was derived from the accepted read-only audit inventory whose
SHA-256 was
`df6630692b0f1f13b6542d7392f68ef77b30ce21772461018853aa22e3b22fe3`.
The versioned snapshot SHA-256 is recorded in
`EVIDENCE_INVENTORY_SNAPSHOT.sha256`.

Portability and sanitization rules:

- `/home/huyy-giaa/` prefixes were normalized to `~/`; no local absolute home
  path is required for rebuilding.
- Fifteen paths whose content hashes were already marked
  `SKIPPED_POTENTIALLY_SENSITIVE_NAME` were replaced with opaque restricted
  path/destination identifiers. Their contents were not opened or included.
- `source_identity_sha256` records the SHA-256 of the exact pre-sanitization
  source identifier. The builder uses it to preserve stable artifact IDs and
  collision disambiguation.
- `path_reference_class` preserves the accepted bounded reference-scan result,
  so sanitization does not alter the reviewed classification.
- `inferred_component` and `canonical_artifact_id` preserve the accepted
  component inference and same-content canonical link when a sanitized path
  would otherwise change lexical/path-token inference.
- The snapshot contains audit metadata only. It does not contain credentials,
  evidence file contents, or the external 4.91 GB evidence collection.

Verify and rebuild from the repository root:

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
```
