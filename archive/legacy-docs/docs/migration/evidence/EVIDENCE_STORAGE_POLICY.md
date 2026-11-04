# Evidence storage policy for Phase B1 review

## Repository evidence

Git may contain compact, non-sensitive evidence that reviewers need directly:

- sanitized screenshots;
- relevant API/JSON readbacks;
- compact metrics and test summaries;
- rubric matrices and explanatory reports;
- evidence manifests and checksum indexes.

The default per-file limit is 10 MiB. An exception requires owner review and a
manifested reason. A compact artifact is still not eligible for Git when its
phase, sensitivity, or provenance is unresolved.

## External durable artifacts

Keep the following outside Git:

- Parquet and large generated datasets;
- model weights and serialized models;
- full benchmark traces and large raw logs;
- database files and compressed bundles;
- any artifact over the default size limit.

The V2 map identifies 134 external references. `~/ewallet-evidence/` is a local
staging/recovery location, not a portable final destination. Phase B1 should
reuse an approved versioned GCS/artifact-store location and record URI,
generation/version, size, checksum, retention, retrieval, and reproduction
instructions. This milestone creates no bucket and uploads nothing.

## Historical and reproducible outputs

- Preserve historical evidence and failed-run logs with their original status.
- Do not commit reproducible caches/build contexts solely because they appear
  beneath an evidence campaign directory.
- A generated output may be excluded when the repository contains a verified
  runbook and immutable inputs sufficient to reproduce it; the manifest must
  record that decision.
- Do not overwrite immutable campaign/run directories.

## Same-content duplicates

Verified same-content sources remain separate manifest records. A later owner
may select one canonical binary and alias manifests only after confirming:

1. identical SHA-256 and size;
2. no path-dependent runtime consumer;
3. full source/run provenance retained;
4. rollback from the pre-migration inventory.

No duplicate is deleted in Phase B0.

## Sensitive artifacts

Rows whose names may contain secrets remain `BLOCKED_FOR_REVIEW`. Do not open,
hash, commit, or upload them through the migration process until the security
owner confirms content and redaction. Manifests must never contain credentials,
tokens, private keys, user data, or signed download URLs.

## Phase B1 gates

Before moving one component:

1. component owner checkpoints active/untracked work;
2. exact source SHA, run ID, status, and rubric mapping are verified;
3. path references are reviewed and updated in the same small commit;
4. checksum before/after matches;
5. manifest schema validation passes;
6. link and focused component tests pass;
7. rollback path is documented;
8. no external placeholder is mistaken for a real durable URI.

Phase B1 requires separate owner approval.
