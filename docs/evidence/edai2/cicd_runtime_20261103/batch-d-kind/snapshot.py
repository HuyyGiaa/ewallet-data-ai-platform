"""Identity snapshot of every object in a Helm release manifest (kind test only)."""
import json, subprocess, sys, yaml
ctx, manifest, out = sys.argv[1], sys.argv[2], sys.argv[3]
rows = {}
for d in yaml.safe_load_all(open(manifest)):
    if not d: continue
    ns, name, kind = d["metadata"]["namespace"], d["metadata"]["name"], d["kind"]
    r = subprocess.run(["kubectl", "--context", ctx, "-n", ns, "get", kind, name, "-o", "json"], capture_output=True, text=True)
    if r.returncode:
        rows[f"{kind}/{ns}/{name}"] = "MISSING"; continue
    o = json.loads(r.stdout); m = o["metadata"]
    rows[f"{kind}/{ns}/{name}"] = {"uid": m["uid"], "generation": m.get("generation"),
        "release": m.get("annotations", {}).get("meta.helm.sh/release-name"),
        "release_ns": m.get("annotations", {}).get("meta.helm.sh/release-namespace"),
        "spec_sha": __import__("hashlib").sha256(json.dumps(o.get("spec"), sort_keys=True).encode()).hexdigest()[:16]}
pods = subprocess.run(["kubectl", "--context", ctx, "-n", "ewallet-mcp", "get", "pods", "-o", "json"], capture_output=True, text=True)
rows["pods"] = sorted((p["metadata"]["name"], p["metadata"]["uid"]) for p in json.loads(pods.stdout)["items"])
json.dump(rows, open(out, "w"), indent=1, sort_keys=True)
print(len(rows) - 1, "objects;", sum(v == "MISSING" for v in rows.values()), "missing;", len(rows["pods"]), "pods")
