"""Workload-identity GCS probe for the rows 11-12 verification bindings (no secrets printed)."""
import hashlib, json, os, sys, urllib.request
from google.api_core import exceptions as gexc
from google.cloud import storage

B = "ewallet-data-ai-platform-hg26-rehearsal-5gb-data"
SNAP = ("features/offline/snapshots/rehearsal-5gb-v1/"
        "ab-20261110225456-optimized-feat-r261110225459/feast-r5gb-20261111-214544")
EXPECTED = "3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca"
RUN = os.environ["PROBE_ID"]
out = {"probe_id": RUN, "checks": {}}
req = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
    headers={"Metadata-Flavor": "Google"})
out["identity"] = urllib.request.urlopen(req, timeout=10).read().decode()
bucket = storage.Client().bucket(B)

def check(name, fn, expect):
    try:
        detail = fn(); got = "OK"
    except gexc.Forbidden:
        detail, got = None, "FORBIDDEN"
    except gexc.PreconditionFailed:
        detail, got = None, "PRECONDITION_FAILED"
    except Exception as error:  # noqa: BLE001
        detail, got = repr(error)[:300], "ERROR"
    out["checks"][name] = {"result": got, "expected": expect, "pass": got == expect, "detail": detail}

def read_manifest():
    raw = bucket.blob(f"{SNAP}/_control/export_manifest.json").download_as_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == EXPECTED, digest
    return digest
check("read_snapshot_manifest", read_manifest, "OK")
check("list_snapshot_prefix", lambda: len(list(storage.Client().list_blobs(B, prefix=f"{SNAP}/feat_user_behavior/", max_results=5))), "OK")
probe = f"features/_iam-probe/{RUN}.json"
check("create_in_probe_prefix", lambda: bucket.blob(probe).upload_from_string(json.dumps({"probe": RUN}), if_generation_match=0) or probe, "OK")
check("recreate_same_object_refused", lambda: bucket.blob(probe).upload_from_string("x", if_generation_match=0), "PRECONDITION_FAILED")
check("create_in_lakehouse_denied", lambda: bucket.blob(f"lakehouse/_iam-negative-probe/{RUN}").upload_from_string("x", if_generation_match=0), "FORBIDDEN")
check("create_in_offline_snapshots_denied", lambda: bucket.blob(f"features/offline/_iam-negative-probe/{RUN}").upload_from_string("x", if_generation_match=0), "FORBIDDEN")
check("list_lakehouse_denied", lambda: len(list(storage.Client().list_blobs(B, prefix="lakehouse/", max_results=1))), "FORBIDDEN")
out["status"] = "PASS" if all(c["pass"] for c in out["checks"].values()) else "FAIL"
print(json.dumps(out, sort_keys=True))
sys.exit(0 if out["status"] == "PASS" else 1)
