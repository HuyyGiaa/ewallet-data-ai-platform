"""Vulnerability gate for release images, by immutable digest (P0 finding CICD-P0-F15).

Jenkins has no Docker daemon, so the scanner runs as a Cloud Build step under the runner identity
(``ewallet-cloudbuild``, which can already read the release repository). The scanner is the same
pinned Trivy digest the repository already uses (``Jenkinsfile.rag``, ``scripts/llm_agent/scan_and_push.sh``).
It always writes its JSON report (``--exit-code 0``), which Cloud Build uploads next to the build
logs; the policy is then decided here, so the three outcomes stay distinct:

* ``PASS``: the report exists and has no HIGH or CRITICAL finding with a fix that is not covered
  by an unexpired reviewed exception;
* ``VULNERABLE``: such a finding exists (each one is listed);
* ``SCANNER_ERROR``: the scan build failed or produced no readable report. Never a pass.

Reviewed exceptions live in ``jenkins/security/vulnerability-exceptions.json``: each names a
vulnerability id, the package, the release images it covers, a reason and an expiry date. It applies
only to a finding of that id and package in one of those images; an expired exception no longer
applies and is reported.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
from urllib.parse import quote

from scripts.ci.cloud_build import TERMINAL, BuildError, Http, Settings

TRIVY = ("docker.io/aquasec/trivy:0.75.0@sha256:"
         "af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa")
SEVERITIES = ("HIGH", "CRITICAL")
DIGEST_REF = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
EXCEPTIONS = Path(__file__).resolve().parents[2] / "jenkins/security/vulnerability-exceptions.json"


@dataclass(frozen=True)
class Exception_:
    vulnerability: str
    package: str | None
    expires: date
    reason: str
    images: tuple[str, ...] = ()


def load_exceptions(path: Path = EXCEPTIONS) -> list[Exception_]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for item in data.get("exceptions", []):
        if not item.get("reason") or not item.get("id") or not item.get("package"):
            raise ValueError("every vulnerability exception needs an id, a package and a reason")
        images = item.get("images")
        if not isinstance(images, list) or not images or not all(isinstance(i, str) and i for i in images):
            raise ValueError(f"exception {item['id']} must name the release images it covers")
        items.append(Exception_(item["id"], item["package"], date.fromisoformat(item["expires"]), item["reason"],
                                tuple(images)))
    return items


def evaluate(report: dict[str, Any], exceptions: list[Exception_], today: date, image: str = "") -> dict[str, Any]:
    findings, excepted, expired = [], [], []
    for result in report.get("Results") or []:
        for vulnerability in result.get("Vulnerabilities") or []:
            if vulnerability.get("Severity") not in SEVERITIES or not vulnerability.get("FixedVersion"):
                continue
            key = {"id": vulnerability.get("VulnerabilityID"), "package": vulnerability.get("PkgName"),
                   "severity": vulnerability.get("Severity"), "target": result.get("Target")}
            match = next((item for item in exceptions if item.vulnerability == key["id"]
                          and item.package in (None, key["package"]) and image in item.images), None)
            if match is not None and match.expires >= today:
                excepted.append(key)
            else:
                if match is not None:
                    expired.append(key)
                findings.append(key)
    return {"status": "VULNERABLE" if findings else "PASS", "findings": findings,
            "excepted": excepted, "expired_exceptions": expired}


def scan_request(settings: Settings, image: str, report_prefix: str) -> dict[str, Any]:
    return {
        "steps": [{"name": TRIVY, "args": ["image", "--quiet", "--format", "json", "--output", "/workspace/trivy.json",
                                            "--severity", ",".join(SEVERITIES), "--ignore-unfixed",
                                            "--exit-code", "0", image]}],
        "artifacts": {"objects": {"location": f"gs://{settings.logs_bucket}/{report_prefix}/", "paths": ["trivy.json"]}},
        "serviceAccount": f"projects/{settings.project}/serviceAccounts/{settings.runner}",
        "logsBucket": f"gs://{settings.logs_bucket}",
        "options": {"logging": "GCS_ONLY"},
        "timeout": "1200s",
        "tags": ["ewallet-jenkins", "image-scan"],
    }


def scan_image(http: Http, settings: Settings, name: str, image: str, git_sha: str,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    if not DIGEST_REF.fullmatch(image):
        raise BuildError(f"{name}: only immutable digests are scanned")
    prefix = f"scans/{git_sha}/{name}/{image.rsplit(':', 1)[1][:16]}"
    base = (f"https://cloudbuild.googleapis.com/v1/projects/{settings.project}"
            f"/locations/{settings.region}/builds")
    try:
        operation = http.call("POST", base, scan_request(settings, image, prefix))
        build_id = operation["metadata"]["build"]["id"]
        while True:
            build = http.call("GET", f"{base}/{build_id}")
            if build.get("status") in TERMINAL:
                break
            sleep(15)
        if build["status"] != "SUCCESS":
            return {"status": "SCANNER_ERROR", "detail": f"scan build {build_id} ended {build['status']}", "image": image}
        raw = http.call("GET", f"https://storage.googleapis.com/storage/v1/b/{settings.logs_bucket}/o/"
                               f"{quote(prefix + '/trivy.json', safe='')}?alt=media", raw=True)
        return {"status": "REPORT", "report": json.loads(raw), "build_id": build_id, "image": image}
    except (BuildError, ValueError, KeyError) as error:
        return {"status": "SCANNER_ERROR", "detail": str(error), "image": image}


def gate(scans: dict[str, dict[str, Any]], exceptions: list[Exception_], today: date,
         git_sha: str | None = None) -> dict[str, Any]:
    """Policy verdict; each image result names the digest it scanned (the handoff binds it to the manifest)."""

    images: dict[str, Any] = {}
    for name, scan in sorted(scans.items()):
        if scan.get("status") != "REPORT":
            images[name] = {"status": "SCANNER_ERROR", "detail": scan.get("detail")}
        else:
            images[name] = evaluate(scan["report"], exceptions, today, image=name)
        if scan.get("image"):
            images[name]["digest_ref"] = scan["image"]
    statuses = {item["status"] for item in images.values()}
    if not scans:
        verdict = "NOT_APPLICABLE"  # the release consumes no image (e.g. the Prompt Management gate)
    else:
        verdict = "PASS" if statuses == {"PASS"} else ("SCANNER_ERROR" if "SCANNER_ERROR" in statuses else "VULNERABLE")
    result = {"verdict": verdict, "scanner": TRIVY, "severities": list(SEVERITIES), "images": images}
    if git_sha is not None:
        result["git_sha"] = git_sha
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="ewallet-data-ai-platform-hg26")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--repository", default="ewallet-platform")
    parser.add_argument("--logs-bucket", default="ewallet-data-ai-platform-hg26-cicd-build-logs")
    parser.add_argument("--runner", default="ewallet-cloudbuild@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com")
    args = parser.parse_args(argv)
    manifest = json.loads(args.image_manifest.read_text(encoding="utf-8"))
    settings = Settings(args.project, args.region, args.repository, "", args.runner, args.logs_bucket)
    http = Http()
    scans = {name: scan_image(http, settings, name, image["digest_ref"], manifest["git_sha"])
             for name, image in manifest.get("images", {}).items()}
    result = gate(scans, load_exceptions(), date.today(), git_sha=manifest["git_sha"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"],
                      "images": {name: item["status"] for name, item in result["images"].items()}}, sort_keys=True))
    return 0 if result["verdict"] in {"PASS", "NOT_APPLICABLE"} else 1


if __name__ == "__main__":
    sys.exit(main())
