"""Offline R1 scenarios through the real agentgateway binary and mock vLLM."""

import json
import sys
import time
import urllib.request

sys.path.insert(0, "/w")
from smoke_tool_call import run  # noqa: E402

GATEWAY = "http://gw:4000"
BACKENDS = {"primary": "http://vllm-primary.ewallet-agents.svc.cluster.local:8000",
            "fallback": "http://vllm-fallback.ewallet-agents.svc.cluster.local:8000"}
KEY = "client-side-key-not-forwarded"


def control(name, mode, delay=0):
    req = urllib.request.Request(BACKENDS[name] + "/_mode", json.dumps({"mode": mode, "delay": delay}).encode(),
                                 {"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def seen(name):
    return json.load(urllib.request.urlopen(BACKENDS[name] + "/_log", timeout=5))


def scenario(label, primary, fallback, *, stream=False, delay=0, settle=0):
    control("primary", primary, delay)
    control("fallback", fallback)
    report = run(GATEWAY, KEY, "ewallet-fraud-llm", tool_choice="required", stream=stream, timeout=30)
    report["backend_requests"] = {"primary": seen("primary"), "fallback": seen("fallback")}
    time.sleep(settle)
    return label, report


SCENARIOS = {
    "ok_non_stream": dict(primary="ok", fallback="ok"),
    "ok_stream": dict(primary="ok", fallback="ok", stream=True),
    "primary_500_not_replayed": dict(primary="500", fallback="ok"),
    "primary_slow_beyond_backend_budget": dict(primary="slow", fallback="ok", delay=8),
    "primary_down_503": dict(primary="503", fallback="ok"),
    "all_backends_down": dict(primary="503", fallback="503"),
}
name = sys.argv[1]
if name == "primary_down_then_failover":
    # First request fails (and its retry): two consecutive failures evict the
    # primary, so the next request is served by the fallback.
    control("primary", "503")
    control("fallback", "ok")
    first = run(GATEWAY, KEY, "ewallet-fraud-llm", tool_choice="required", stream=False, timeout=30)
    report = run(GATEWAY, KEY, "ewallet-fraud-llm", tool_choice="required", stream=False, timeout=30)
    report["preceding_request_status"] = first["first_turn"].get("status")
    report["backend_requests"] = {"primary": seen("primary"), "fallback": seen("fallback")}
    print(json.dumps(report, sort_keys=True))
    sys.exit(0)
spec = SCENARIOS[name]
label, report = scenario(name, spec["primary"], spec["fallback"], stream=spec.get("stream", False),
                         delay=spec.get("delay", 0))
print(json.dumps(report, sort_keys=True))
