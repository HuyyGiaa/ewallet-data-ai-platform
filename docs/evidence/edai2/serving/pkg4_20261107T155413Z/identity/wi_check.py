import json, os, urllib.request
from google.cloud import storage
from google.api_core import exceptions as ex
out = {}
req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email", headers={"Metadata-Flavor": "Google"})
out["identity"] = urllib.request.urlopen(req, timeout=10).read().decode()
client = storage.Client(project="ewallet-data-ai-platform-hg26")
reg = os.environ["REG_URI"][5:]; bucket, _, name = reg.partition("/")
try:
    data = client.bucket(bucket).blob(name).download_as_bytes(); out["registry_read"] = f"OK {len(data)} bytes"
except Exception as e: out["registry_read"] = f"DENIED {type(e).__name__}"
other = "ml/training-snapshots/canonical-100gb-v1/training-20261031-151638-ml2/manifest.json"
try:
    client.bucket(bucket).blob(other).download_as_bytes(); out["outside_prefix_read"] = "ALLOWED (unexpected)"
except ex.Forbidden: out["outside_prefix_read"] = "DENIED 403 (expected)"
except Exception as e: out["outside_prefix_read"] = f"ERROR {type(e).__name__}"
try:
    next(iter(client.list_blobs(bucket, prefix="features/registries/", max_results=1))); out["bucket_list"] = "ALLOWED (unexpected)"
except ex.Forbidden: out["bucket_list"] = "DENIED 403 (expected)"
except StopIteration: out["bucket_list"] = "ALLOWED empty (unexpected)"
except Exception as e: out["bucket_list"] = f"ERROR {type(e).__name__}"
print(json.dumps(out))
