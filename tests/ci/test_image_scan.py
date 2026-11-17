"""Image vulnerability gate: three distinct outcomes, reviewed expiring exceptions, digest-only scans."""

from __future__ import annotations

from datetime import date
import json

from scripts.ci.cloud_build import Settings
from scripts.ci.image_scan import TRIVY, Exception_, evaluate, gate, load_exceptions, scan_image, scan_request

TODAY = date(2026, 11, 18)
IMAGE = "asia-southeast1-docker.pkg.dev/p/ewallet-platform/mcp-tools@sha256:" + "a" * 64
SETTINGS = Settings("p", "asia-southeast1", "ewallet-platform", "", "runner@p.iam.gserviceaccount.com", "logs")


def report(*vulnerabilities: dict) -> dict:
    return {"Results": [{"Target": "image", "Vulnerabilities": list(vulnerabilities)}]}


HIGH = {"VulnerabilityID": "CVE-1", "PkgName": "openssl", "Severity": "HIGH", "FixedVersion": "3.0.1"}


def test_clean_and_unfixable_reports_pass() -> None:
    assert evaluate(report(), [], TODAY)["status"] == "PASS"
    assert evaluate(report({**HIGH, "FixedVersion": ""}), [], TODAY)["status"] == "PASS"
    assert evaluate(report({**HIGH, "Severity": "MEDIUM"}), [], TODAY)["status"] == "PASS"


def test_a_fixable_high_finding_is_vulnerable() -> None:
    result = evaluate(report(HIGH), [], TODAY)
    assert result["status"] == "VULNERABLE" and result["findings"][0]["id"] == "CVE-1"


def test_only_an_unexpired_matching_exception_applies() -> None:
    valid = Exception_("CVE-1", "openssl", date(2026, 12, 31), "reviewed")
    expired = Exception_("CVE-1", None, date(2026, 11, 1), "old")
    other_package = Exception_("CVE-1", "zlib", date(2026, 12, 31), "reviewed")
    assert evaluate(report(HIGH), [valid], TODAY)["status"] == "PASS"
    result = evaluate(report(HIGH), [expired], TODAY)
    assert result["status"] == "VULNERABLE" and result["expired_exceptions"]
    assert evaluate(report(HIGH), [other_package], TODAY)["status"] == "VULNERABLE"


def test_scanner_errors_never_pass_and_dominate_the_verdict() -> None:
    result = gate({"a": {"status": "REPORT", "report": report()}, "b": {"status": "SCANNER_ERROR", "detail": "x"}},
                  [], TODAY)
    assert result["verdict"] == "SCANNER_ERROR"
    assert gate({}, [], TODAY)["verdict"] != "PASS"


def test_the_scan_is_pinned_and_writes_a_report_artifact() -> None:
    request = scan_request(SETTINGS, IMAGE, "scans/x")
    step = request["steps"][0]
    assert step["name"] == TRIVY and "@sha256:" in TRIVY
    assert "--exit-code" in step["args"] and step["args"][step["args"].index("--exit-code") + 1] == "0"
    assert step["args"][-1] == IMAGE
    assert request["artifacts"]["objects"]["paths"] == ["trivy.json"]


def test_only_digests_are_scanned_and_a_failed_scan_build_is_a_scanner_error() -> None:
    class Http:
        def __init__(self, status):  # type: ignore[no-untyped-def]
            self.status = status

        def call(self, method, url, body=None, raw=False):  # type: ignore[no-untyped-def]
            if method == "POST":
                return {"metadata": {"build": {"id": "s-1"}}}
            if url.endswith("/builds/s-1"):
                return {"status": self.status}
            return json.dumps(report()).encode()

    try:
        scan_image(Http("SUCCESS"), SETTINGS, "mcp-tools", "registry/x:latest", "s" * 40)
        raise AssertionError("a tag was scanned")
    except Exception as error:  # BuildError
        assert "immutable digests" in str(error)
    assert scan_image(Http("FAILURE"), SETTINGS, "mcp-tools", IMAGE, "s" * 40, sleep=lambda _: None)["status"] == "SCANNER_ERROR"
    assert scan_image(Http("SUCCESS"), SETTINGS, "mcp-tools", IMAGE, "s" * 40, sleep=lambda _: None)["status"] == "REPORT"


def test_the_committed_exception_list_is_valid() -> None:
    assert load_exceptions() == []
