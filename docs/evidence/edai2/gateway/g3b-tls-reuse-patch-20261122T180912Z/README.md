# G3-B TLS certificate reuse patch (offline)

Base 9430b837. No cloud change. Opt-in `--reuse-existing-production-certificate` keeps the trusted
Let's Encrypt certificate that run 6 (`g3b-full-20261122T164612Z`) put into `ewallet-ingress/edge-tls`,
instead of importing a self-signed one and ordering a new certificate on every campaign.

## Stage A (read-only, live Secret, GKE asleep)

`verify_edge_tls.py` on the live `edge-tls`: `TLS_REUSE_VERIFIED`.
- issuer Let's Encrypt YE1; valid until 2027-02-20 (about 89 days; a 100-day requirement is correctly refused);
- all 4 names; chain trusted by the system CA bundle (3 chain certificates);
- the key matches the certificate (public-key hash; the key was never printed or written);
- `edge-tls` is byte-identical to the certificate run 6 issued. The Secret survives sleep and wake.

## Patch

| File | Change |
|---|---|
| `scripts/edge/verify_edge_tls.py` (new) | read-only verifier: Secret read into memory; checks a trusted chain (system bundle), the 4 names, at least N days left, key matches (public key derived via openssl on stdin). One JSON verdict line, exit 0 / 1 / 2 |
| `scripts/edge/g3b_campaign.py` | flag `--reuse-existing-production-certificate` (`config.tls_mode=reuse`); init gate `TLS_REUSE_READY` (verifier + production approval) instead of `CERTBOT_READY`; phase `tls-reuse-verify` instead of `temporary-certificate`; edge install and L0 render with `values_final` (no ACME responder); private, Internet and L4 checks without `--ca-file` (system trust, real hostnames); staging and production record `SKIPPED_REUSE`, attempts 0; state fields `TLS_MODE`, `TLS_CERTIFICATE_STATUS`, `TLS_CERTIFICATE_SOURCE`; verdicts accept a verified reuse in place of issuance and report `TLS_MODE` / `TLS_CERTIFICATE_STATUS`. Issue mode is unchanged |
| runbook | short section on reuse mode |

Fail closed: a missing, expiring, untrusted, wrong-name, mismatched or unreadable certificate stops
the campaign. There is no self-signed fallback and no automatic issuance, and cleanup never touches `edge-tls`.

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_g3b_tls_reuse.py` (new) | 16 passed: verifier valid (no key material printed), missing SAN, near expiry, untrusted, key mismatch, missing key, garbage, unreadable Secret; campaign reuse (no issue_certificate / certbot / secret import, SKIPPED_REUSE, attempts 0, values_final, verdicts PASS / COMPLETE), no `--ca-file` / insecure flags, refused certificate fails closed with cleanup, approval required, legacy issue mode unchanged, late failure never touches edge-tls, Grafana readiness before the final E2E, reuse vs issuance distinguishable, L0 render values |
| G3-B regression (13 files) | 484 passed |
| full `tests/ops` | 679 passed |
