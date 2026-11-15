"""Build the runtime-stability images from the pinned kagent/Substrate tags plus our patches.

    python3 scripts/llm_agent/build_runtime_fix_images.py --out <dir> [--skip-tests]

Reads infra/kubernetes/kagent-pilot/patches/runtime-fix.json. For each component it downloads the
release tarball, checks its sha256, applies the patch (sha256-checked) and the pinned security
module bumps (go.mod/go.sum hash-checked), runs the patched packages' tests, builds the binary in the digest-pinned Go image with the release's own build
flags, and builds an image FROM the official release image digest with only that binary
replaced. Writes <out>/image-digests.json for scripts/llm_agent/scan_and_push.sh (Trivy gate,
push, digest). Needs ARTIFACT_REGISTRY=<region>-docker.pkg.dev/<project>/<repository>.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "infra/kubernetes/kagent-pilot/patches"
REGISTRY = re.compile(r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+$")

# Tests of the patched packages, run in the component's Go image before anything is built.
TESTS = {
    "kagent": [("go", "go build ./... && go vet ./core/internal/a2a/ ./core/pkg/sandboxbackend/... && "
                      "go test -race -count=1 -short ./core/internal/a2a/... ./core/pkg/sandboxbackend/... ./core/internal/httpserver/...", False)],
    "substrate": [
        (".", "go build ./... && go vet ./cmd/... && go test -race -count=1 ./cmd/ateapi/... ./cmd/atelet/... ./internal/...", False),
        (".", "go test -race -count=1 ./cmd/ateom-gvisor/", True),  # needs root for /var/lib/ateom-gvisor
    ],
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def fetch_source(component: dict, work: Path, name: str) -> Path:
    tarball = work / f"{name}.tar.gz"
    if not tarball.exists():
        with urllib.request.urlopen(component["source"], timeout=120) as response:  # noqa: S310 - pinned https URL
            tarball.write_bytes(response.read())
    if sha256(tarball) != component["source_sha256"]:
        raise SystemExit(f"{name}: source sha256 mismatch")
    patch = PATCHES / component["patch"]
    if sha256(patch) != component["patch_sha256"]:
        raise SystemExit(f"{name}: patch sha256 mismatch")
    dest = work / f"{name}-src"
    with tarfile.open(tarball) as archive:
        archive.extractall(dest, filter="data")
    (src,) = list(dest.iterdir())
    run(["patch", "-p1", "--forward", "--batch", "-i", str(patch)], cwd=src)
    # Substrate's envtest helper finds the repository root with git.
    run(["git", "init", "-q"], cwd=src)
    return src


def security_bumps(component: dict, src: Path, out: Path, name: str) -> None:
    """Apply the pinned module bumps (go get + vendor) and verify the resulting go.mod/go.sum."""
    bumps = component["security_bumps"]
    go_image = next(iter(component["images"].values()))["go_image"]
    mod_dir = src / bumps["module_dir"]
    before = (mod_dir / "go.mod").read_text()
    cmd = "go get " + " ".join(bumps["go_get"]) + (" && go mod vendor" if bumps["vendor"] else "")
    go_container(go_image, src, bumps["module_dir"], {"GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local"}, cmd)
    for f in ("go.mod", "go.sum"):
        if sha256(mod_dir / f) != bumps[f"{f.replace('.', '_')}_sha256"]:
            raise SystemExit(f"{name}: {f} after the security bumps is not the reviewed one")
    after = (mod_dir / "go.mod").read_text()
    diff = subprocess.run(["diff", "-u", "-", str(mod_dir / "go.mod")], input=before, capture_output=True, text=True).stdout
    (out / f"{name}-go.mod.diff").write_text(diff if after != before else "")


def go_container(go_image: str, src: Path, workdir: str, env: dict[str, str], cmd: str, *, root: bool = False,
                 extra: list[str] | None = None) -> None:
    user = [] if root else ["--user", f"{os.getuid()}:{os.getgid()}"]
    envs = [f"-e{k}={v}" for k, v in env.items()]
    run(["docker", "run", "--rm", "--network", "host", *user, *envs, "-e", "HOME=/tmp", "-e", "GOCACHE=/tmp/gocache",
         "-e", "GOPATH=/tmp/gopath", *(extra or []), "-v", f"{src}:/src", "-w", f"/src/{workdir}",
         go_image, "bash", "-c", cmd])


def ldflags(image: dict, patch_sha: str) -> str:
    mark = "+ewallet." + patch_sha[:8]
    flags = image["ldflags_from_base"]
    flags = re.sub(r"(version\.GitCommit=)(\S+)", lambda m: m.group(1) + m.group(2) + mark, flags)
    return re.sub(r"(version\.Commit=)(\S+)", lambda m: m.group(1) + m.group(2) + mark, flags)


def build(args: argparse.Namespace) -> dict:
    registry = os.environ.get("ARTIFACT_REGISTRY", "")
    if not REGISTRY.match(registry):
        raise SystemExit("ARTIFACT_REGISTRY must be <region>-docker.pkg.dev/<project>/<repository>")
    spec = json.loads((PATCHES / "runtime-fix.json").read_text())
    git_sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True,
                             text=True).stdout.strip()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="runtime-fix-", dir=out))
    result = {"git_sha": git_sha, "spec_sha256": sha256(PATCHES / "runtime-fix.json"), "images": {}}
    for name in ("kagent", "substrate"):
        component = spec[name]
        src = fetch_source(component, work, name)
        security_bumps(component, src, out, name)
        if not args.skip_tests:
            go_image = next(iter(component["images"].values()))["go_image"]
            for workdir, cmd, root in TESTS[name]:
                env = {"GOFLAGS": "-mod=mod" if name == "kagent" else "-mod=vendor", "GOTOOLCHAIN": "local"}
                go_container(go_image, src, workdir, env, cmd, root=root)
        for image_name, image in component["images"].items():
            binary = f"/src/.out/{image_name}"
            go_container(image["go_image"], src, image["module_dir"], {**image["env"], "GOTOOLCHAIN": "local"},
                         f"go build -ldflags {json.dumps(ldflags(image, component['patch_sha256']))} -o {binary} {image['package']}")
            local = src / ".out" / image_name
            tag = f"{registry}/{image_name}:{component['version']}-ewallet.{component['patch_sha256'][:8]}"
            context = work / f"ctx-{image_name}"
            context.mkdir()
            (context / "binary").write_bytes(local.read_bytes())
            (context / "binary").chmod(0o755)
            (context / "Dockerfile").write_text(
                f"FROM {image['base']}\n"
                f"COPY --chmod=0755 binary {image['binary_path']}\n"
                f"LABEL org.opencontainers.image.base.name=\"{image['base']}\" \\\n"
                f"      org.opencontainers.image.source=\"{component['source']}\" \\\n"
                f"      org.opencontainers.image.revision=\"{component['tag_commit']}\" \\\n"
                f"      dev.ewallet.patch=\"{component['patch']}\" \\\n"
                f"      dev.ewallet.patch-sha256=\"{component['patch_sha256']}\" \\\n"
                f"      dev.ewallet.repo-revision=\"{git_sha}\"\n")
            run(["docker", "build", "-q", "-t", tag, str(context)])
            result["images"][image_name] = {
                "tag_ref": tag, "digest_ref": None, "base": image["base"], "binary_sha256": sha256(local),
                "upstream": f"{name} v{component['version']} ({component['tag_commit']})",
                "source_sha256": component["source_sha256"], "patch_sha256": component["patch_sha256"],
                "go_image": image["go_image"], "security_bumps": component["security_bumps"]["go_get"],
                "tests": "skipped" if args.skip_tests else "passed",
            }
    path = out / "image-digests.json"
    path.write_text(json.dumps(result, indent=1, sort_keys=True))
    print(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-tests", action="store_true", help="only for re-builds of an already tested patch")
    build(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
