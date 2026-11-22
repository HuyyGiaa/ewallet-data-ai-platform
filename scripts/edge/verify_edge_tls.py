"""Read-only check that ewallet-ingress/edge-tls holds a reusable, publicly trusted certificate.

Used by g3b_campaign.py --reuse-existing-production-certificate instead of issuing a new one.
The Secret is read into memory only; the private key is passed to openssl on stdin to derive its
public key and is never written, printed or recorded. Output is one JSON line without key material.

  exit 0  TLS_REUSE_VERIFIED       chain trusted by the CA bundle, all four names, at least
                                   --min-days left, key matches the certificate
  exit 1  TLS_REUSE_REFUSED        readable, but not safe to reuse (reason given)
  exit 2  TLS_REUSE_CHECK_FAILED   the Secret or a tool could not be read
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HOSTS = ("features", "agents", "registry", "observe")
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----\s*", re.S)


def openssl(*args: str, data: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=30)


def verdict(code: int, name: str, **fields) -> int:
    print(json.dumps({"verdict": name, **fields}))
    return code


def check(crt: bytes, key: bytes, domain: str, min_days: int, ca_bundle: str) -> int:
    certs = PEM.findall(crt)
    if not certs:
        return verdict(1, "TLS_REUSE_REFUSED", reason="tls.crt holds no certificate")
    leaf, chain = certs[0], b"".join(certs[1:])
    info = openssl("x509", "-noout", "-issuer", "-enddate", "-ext", "subjectAltName", data=leaf)
    if info.returncode:
        return verdict(1, "TLS_REUSE_REFUSED", reason="leaf certificate not parseable")
    text = info.stdout.decode()
    issuer = next((l.split("=", 1)[1].strip() for l in text.splitlines() if l.startswith("issuer=")), "")
    not_after = next((l.split("=", 1)[1].strip() for l in text.splitlines() if l.startswith("notAfter=")), "")
    names = set(re.findall(r"DNS:([^,\s]+)", text))
    missing = sorted(f"{h}.{domain}" for h in HOSTS if f"{h}.{domain}" not in names)
    facts = {"issuer": issuer, "not_after": not_after, "names": sorted(names)}
    if missing:
        return verdict(1, "TLS_REUSE_REFUSED", reason=f"missing names {missing}", **facts)
    if openssl("x509", "-noout", "-checkend", str(min_days * 86400), data=leaf).returncode:
        return verdict(1, "TLS_REUSE_REFUSED", reason=f"expires within {min_days} days (or already expired)", **facts)
    with tempfile.TemporaryDirectory() as tmp:     # public certificates only; the key never touches disk
        leaf_file, chain_file = Path(tmp, "leaf.pem"), Path(tmp, "chain.pem")
        leaf_file.write_bytes(leaf)
        args = ["verify", "-CAfile", ca_bundle, "-purpose", "sslserver"]
        if chain:
            chain_file.write_bytes(chain)
            args += ["-untrusted", str(chain_file)]
        trusted = openssl(*args, str(leaf_file))
    if trusted.returncode:
        return verdict(1, "TLS_REUSE_REFUSED", reason="chain not trusted by the CA bundle", **facts)
    cert_pub = openssl("x509", "-noout", "-pubkey", data=leaf)
    key_pub = openssl("pkey", "-pubout", data=key)
    if cert_pub.returncode or key_pub.returncode or not key_pub.stdout:
        return verdict(1, "TLS_REUSE_REFUSED", reason="private key not readable", **facts)
    if hashlib.sha256(cert_pub.stdout).digest() != hashlib.sha256(key_pub.stdout).digest():
        return verdict(1, "TLS_REUSE_REFUSED", reason="private key does not match the certificate", **facts)
    return verdict(0, "TLS_REUSE_VERIFIED", **facts, min_days=min_days, chain_certs=len(certs) - 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="ewallet-ingress")
    parser.add_argument("--secret", default="edge-tls")
    parser.add_argument("--min-days", type=int, default=14)
    parser.add_argument("--ca-bundle", default=SYSTEM_CA, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)
    cmd = ["kubectl", *(["--kubeconfig", args.kubeconfig] if args.kubeconfig else []), "--context", args.context,
           "-n", args.namespace, "get", "secret", args.secret, "-o", "json"]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=60)
        if done.returncode:
            return verdict(2, "TLS_REUSE_CHECK_FAILED", reason=f"cannot read secret {args.namespace}/{args.secret}")
        data = json.loads(done.stdout).get("data") or {}
        crt, key = base64.b64decode(data.get("tls.crt", "")), base64.b64decode(data.get("tls.key", ""))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return verdict(2, "TLS_REUSE_CHECK_FAILED", reason="secret unreadable")
    if not crt or not key:
        return verdict(1, "TLS_REUSE_REFUSED", reason="secret lacks tls.crt or tls.key")
    try:
        return check(crt, key, args.domain, args.min_days, args.ca_bundle)
    finally:
        key = b""   # drop the reference


if __name__ == "__main__":
    sys.exit(main())
