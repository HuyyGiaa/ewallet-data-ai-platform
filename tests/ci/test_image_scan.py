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
    valid = Exception_("CVE-1", "openssl", date(2026, 12, 31), "reviewed", ("mcp-tools",))
    expired = Exception_("CVE-1", "openssl", date(2026, 11, 1), "old", ("mcp-tools",))
    other_package = Exception_("CVE-1", "zlib", date(2026, 12, 31), "reviewed", ("mcp-tools",))
    assert evaluate(report(HIGH), [valid], TODAY, image="mcp-tools")["status"] == "PASS"
    result = evaluate(report(HIGH), [expired], TODAY, image="mcp-tools")
    assert result["status"] == "VULNERABLE" and result["expired_exceptions"]
    assert evaluate(report(HIGH), [other_package], TODAY, image="mcp-tools")["status"] == "VULNERABLE"


def test_an_exception_never_applies_to_another_image() -> None:
    scoped = Exception_("CVE-1", "openssl", date(2026, 12, 31), "reviewed", ("rag-ingestion",))
    assert evaluate(report(HIGH), [scoped], TODAY, image="mcp-tools")["status"] == "VULNERABLE"
    assert evaluate(report(HIGH), [scoped], TODAY)["status"] == "VULNERABLE"
    verdict = gate({"rag-ingestion": {"status": "REPORT", "report": report(HIGH)},
                    "mcp-tools": {"status": "REPORT", "report": report(HIGH)}}, [scoped], TODAY)
    assert verdict["images"]["rag-ingestion"]["status"] == "PASS"
    assert verdict["images"]["mcp-tools"]["status"] == "VULNERABLE" and verdict["verdict"] == "VULNERABLE"


def test_an_exception_must_name_its_package_and_images(tmp_path) -> None:
    base = {"id": "CVE-1", "package": "openssl", "images": ["mcp-tools"], "reason": "r", "expires": "2026-12-20"}
    for broken in ({k: v for k, v in base.items() if k != "package"}, {**base, "images": []},
                   {k: v for k, v in base.items() if k != "images"}, {**base, "images": "mcp-tools"}):
        path = tmp_path / "exceptions.json"
        path.write_text(json.dumps({"exceptions": [broken]}))
        try:
            load_exceptions(path)
            raise AssertionError(f"accepted {broken}")
        except ValueError:
            pass


def test_scanner_errors_never_pass_and_dominate_the_verdict() -> None:
    result = gate({"a": {"status": "REPORT", "report": report()}, "b": {"status": "SCANNER_ERROR", "detail": "x"}},
                  [], TODAY)
    assert result["verdict"] == "SCANNER_ERROR"


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
    assert all(item.images and item.package for item in load_exceptions())


def test_a_release_without_images_is_not_applicable_not_a_pass_or_failure() -> None:
    assert gate({}, [], TODAY)["verdict"] == "NOT_APPLICABLE"
