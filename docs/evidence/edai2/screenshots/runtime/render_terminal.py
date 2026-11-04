"""Render genuine command output (raw/*.txt) to labelled PNGs. The text is not edited;
long lines are wrapped visually. Header states it is a terminal render, not a native UI."""
import html, os, sys, time
from playwright.sync_api import sync_playwright
R = os.path.dirname(os.path.abspath(__file__)); raw = os.path.join(R, "raw")
meta = open(os.path.join(raw, "_meta.txt")).read().strip()
TITLES = {
 "01_agents_replicas_hpa_pdb": "Agents: multi-replica Deployments, HPA autoscaling, PodDisruptionBudgets (edai-2 rows 16, 19, 25)",
 "02_kagent_agent_crs": "kagent Agent CRs: BYO Coordinator/Specialist and Declarative drift-specialist (edai-2 rows 16, 22, 25)",
 "03_sandbox_substrate": "Sandbox: Substrate CRDs, gVisor SandboxConfig, WorkerPool, pilot SandboxAgent (edai-2 rows 20, 27)",
 "04_mcp_rollingupdate": "MCP servers deployed with Helm and RollingUpdate maxUnavailable 0 / maxSurge 1 (edai-2 row 18)",
 "05_kserve_isvc": "KServe InferenceService and predictor pod, model-inside-image (edai-2 row 15)",
 "06_kserve_v2_ready_metadata": "KServe V2 protocol: model ready and metadata (edai-2 row 15)",
 "07_fraud_api_predict_via_kserve": "Fraud API -> Feast -> KServe prediction with correlated KServe log line (edai-2 rows 15, 16)",
 "08_fraud_api_pydantic_and_health": "Fraud API: Kubernetes health endpoints and Pydantic validation 422 (edai-2 rows 16, 17)",
}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 800})
    for name, title in TITLES.items():
        body = open(os.path.join(raw, name + ".txt")).read()
        doc = f"""<html><body style="margin:0;background:#0d1117;color:#c9d1d9;font-family:DejaVu Sans Mono,monospace">
<div style="background:#161b22;padding:12px 16px;border-bottom:1px solid #30363d">
<div style="font:600 17px sans-serif;color:#f0f6fc">{html.escape(title)}</div>
<div style="font:13px sans-serif;color:#8b949e;margin-top:4px">TERMINAL RENDER of real read-only command output (not a native UI) &middot; {html.escape(meta)}</div></div>
<pre style="margin:0;padding:14px 16px;font-size:13px;line-height:1.35;white-space:pre-wrap;word-break:break-all">{html.escape(body)}</pre></body></html>"""
        pg.set_content(doc); time.sleep(0.3)
        pg.screenshot(path=os.path.join(R, name + ".png"), full_page=True)
        print("rendered", name)
    b.close()
