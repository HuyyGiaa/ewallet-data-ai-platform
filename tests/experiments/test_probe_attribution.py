"""Attribution of the policy probe's HTTP 503s from counter snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from ops.experiments.probe_attribution import attribute, main, parse

GKE_BURST = {"cases": [{"case": "rate_limit_burst", "status_counts": {"200": 8, "429": 4, "503": 4}}]}


def vllm(rejected: int, success: int) -> str:
    return (f'vllm:admission_rejections_total{{model_name="ewallet-fraud-llm-v1",reason="max_num_queued_reqs"}} '
            f'{rejected}.0\n'
            'vllm:admission_rejections_total{model_name="ewallet-fraud-llm-v1",reason="max_num_queued_tokens"} 0.0\n'
            f'vllm:request_success_total{{engine="0",finished_reason="length",model_name="m"}} {success}.0\n')


def epp(errors: int, code: str = "ServiceUnavailable") -> str:
    return (f'llm_d_epp_request_error_total{{error_code="{code}",model_name="m"}} {errors}\n'
            'llm_d_epp_request_total{model_name="m"} 40\n') if errors >= 0 else ""


def write(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def test_parse_reads_labels_and_skips_comments() -> None:
    samples = parse("# HELP x\n# TYPE x counter\n" + vllm(3, 7))
    assert samples[("vllm:admission_rejections_total",
                    (("model_name", "ewallet-fraud-llm-v1"), ("reason", "max_num_queued_reqs")))] == 3.0


def test_vllm_queue_cap_explains_the_gke_503s(tmp_path) -> None:
    snaps = write(tmp_path / "s", {
        "vllm-pod-a-before.prom": vllm(0, 10), "vllm-pod-a-after.prom": vllm(4, 17),
        "vllm-pod-b-before.prom": vllm(0, 10), "vllm-pod-b-after.prom": vllm(0, 11),
        "epp-before.prom": epp(0), "epp-after.prom": epp(0)})
    report = attribute(GKE_BURST, snaps)
    assert report["verdict"] == "ATTRIBUTED"
    assert report["component"] == "vLLM admission control (max_num_queued_reqs) on pod-a"
    assert report["vllm"]["pod-a"]["admission_rejections"]["max_num_queued_reqs"] == 4.0


def test_epp_errors_explain_503s_only_when_vllm_rejected_nothing(tmp_path) -> None:
    snaps = write(tmp_path / "s", {
        "vllm-pod-a-before.prom": vllm(0, 10), "vllm-pod-a-after.prom": vllm(0, 18),
        "epp-before.prom": epp(1), "epp-after.prom": epp(5)})
    report = attribute(GKE_BURST, snaps)
    assert report["verdict"] == "ATTRIBUTED" and "llm-d EPP" in report["component"]


def test_vllm_rejection_relayed_by_epp_is_attributed_to_vllm(tmp_path) -> None:
    # EXP-V2 optimized arm, 2026-11-09: vLLM +4 rejections and EPP +4 ModelServerError for 4 503s.
    snaps = write(tmp_path / "s", {
        "vllm-pod-a-before.prom": vllm(0, 10), "vllm-pod-a-after.prom": vllm(4, 22),
        "vllm-pod-b-before.prom": vllm(0, 10), "vllm-pod-b-after.prom": vllm(0, 10),
        "epp-before.prom": epp(0, "ModelServerError"), "epp-after.prom": epp(4, "ModelServerError")})
    report = attribute(GKE_BURST, snaps)
    assert report["verdict"] == "ATTRIBUTED"
    assert report["component"] == ("vLLM admission control (max_num_queued_reqs) on pod-a, "
                                   "relayed by the llm-d EPP as ModelServerError")
    other = write(tmp_path / "o", {
        "vllm-pod-a-before.prom": vllm(0, 10), "vllm-pod-a-after.prom": vllm(4, 22),
        "epp-before.prom": epp(0), "epp-after.prom": epp(4)})
    assert attribute(GKE_BURST, other)["verdict"] == "UNATTRIBUTED"


def test_mismatch_or_missing_snapshot_stays_unattributed(tmp_path) -> None:
    mismatch = write(tmp_path / "m", {"vllm-pod-a-before.prom": vllm(0, 10), "vllm-pod-a-after.prom": vllm(2, 16)})
    assert attribute(GKE_BURST, mismatch)["verdict"] == "UNATTRIBUTED"
    missing = write(tmp_path / "x", {"vllm-pod-a-before.prom": vllm(0, 10), "vllm-pod-a-after.prom": ""})
    assert attribute(GKE_BURST, missing)["verdict"] == "UNATTRIBUTED"
    assert attribute(GKE_BURST, tmp_path / "empty")["verdict"] == "UNATTRIBUTED"


def test_no_503_needs_no_attribution(tmp_path, capsys) -> None:
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"cases": [{"case": "rate_limit_burst", "status_counts": {"200": 12, "429": 4}}]}))
    out = tmp_path / "attribution.json"
    assert main(["--results", str(results), "--snapshots", str(tmp_path / "none"), "--output", str(out)]) == 0
    assert json.loads(out.read_text())["verdict"] == "NO_503"
    assert "NO_503" in capsys.readouterr().out
