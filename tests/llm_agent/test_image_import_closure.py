"""Every ai_platform module an image's entrypoint can import is copied into that image.

The MCP and BYO Dockerfiles copy hand-picked files of ai_platform/features and ai_platform/ml.
On GKE (agi-20261115T145608Z, PKG-P1) feature-rag-mcp crashed at start-up with
``ImportError: cannot import name 'redis_isolation'``: online_target.py imported a module that
the Dockerfile never copied, and no test noticed because every test runs from the full tree.

This test walks the static import graph from each entrypoint, including imports inside
functions (that one was), resolves ``ai_platform.*`` names to files, and requires each file to be
under a COPY source. Modules reachable only through a writer path the image never runs are listed
in NOT_AT_RUNTIME with the reason.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]

IMAGES = {
    "infra/docker/llm-agent-tooling/Dockerfile": ["ai_platform/llm_agent/tools/service.py"],
    "infra/docker/g3-byo-coordinator/Dockerfile": [
        "ai_platform/llm_agent/byo_coordinator.py",
        "ai_platform/llm_agent/byo_feature_rag_specialist.py",
        "ai_platform/llm_agent/byo_drift_specialist.py",
    ],
}

# Reachable in the static graph, never imported by these images at runtime.
NOT_AT_RUNTIME = {
    # Imported only inside rag.feast_publication.apply_and_materialize, the RAG publishing writer;
    # the MCP servers read the published project through repo_config().
    "ai_platform/features/feast/feature_repo/rag_feature_definitions.py",
}


def copied_sources(dockerfile: Path) -> list[str]:
    sources = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"COPY\s+(.+)", line.strip())
        if not match or "--from=" in match.group(1):
            continue
        parts = [p for p in match.group(1).split() if not p.startswith("--")]
        sources += [p.rstrip("/") for p in parts[:-1]]
    return sources


def resolve(module: str) -> str | None:
    path = Path(*module.split("."))
    if (ROOT / path).with_suffix(".py").is_file():
        return str(path.with_suffix(".py"))
    if (ROOT / path / "__init__.py").is_file():
        return str(path / "__init__.py")
    return None


def closure(entrypoints: list[str]) -> dict[str, str]:
    """File -> the file that first imports it."""
    parent: dict[str, str] = {e: "<entrypoint>" for e in entrypoints}
    todo = list(entrypoints)
    while todo:
        current = todo.pop()
        if current in NOT_AT_RUNTIME:
            continue
        for node in ast.walk(ast.parse((ROOT / current).read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                if not name.startswith("ai_platform"):
                    continue
                target = resolve(name)
                if target and target not in parent:
                    parent[target] = current
                    todo.append(target)
    return parent


class ImageImportClosureTests(unittest.TestCase):
    def test_entrypoint_imports_are_copied(self):
        for dockerfile, entrypoints in IMAGES.items():
            sources = copied_sources(ROOT / dockerfile)
            missing = sorted(
                f"{path} (imported by {by})" for path, by in closure(entrypoints).items()
                if path not in NOT_AT_RUNTIME and not any(path == s or path.startswith(s + "/") for s in sources)
            )
            with self.subTest(dockerfile=dockerfile):
                self.assertEqual(missing, [], f"{dockerfile} does not copy modules its entrypoint imports")

    def test_the_exemptions_are_still_writer_only(self):
        publication = (ROOT / "ai_platform/llm_agent/rag/feast_publication.py").read_text(encoding="utf-8")
        tree = ast.parse(publication)
        for function in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
            imports = [m.module for m in ast.walk(function) if isinstance(m, ast.ImportFrom)]
            if "ai_platform.features.feast.feature_repo.rag_feature_definitions" in imports:
                self.assertEqual(function.name, "apply_and_materialize")


if __name__ == "__main__":
    unittest.main()
