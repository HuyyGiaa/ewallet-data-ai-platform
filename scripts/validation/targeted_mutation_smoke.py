"""Bounded, hand-written mutation smoke for the Phase A-D safety checks.

Each mutant is applied to a throwaway copy of the repository, never to the
working tree, and the owning test module is run once. This is a targeted
sample, not a mutmut campaign: its score must not be reported as the EDAI-2
row 30 mutation score.

usage: targeted_mutation_smoke.py <system-python> <fastapi-python> <report.json>
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TOOLS = "ai_platform/llm_agent/tools"
D_TESTS = ("fastapi", "tests.llm_agent.test_phase_d_tools")
SEC_TESTS = ("fastapi", "tests.llm_agent.test_phase_d_security")
P0_TESTS = ("system", "tests.llm_agent.test_phase_abc_p0_fixes")
MUTANTS = (
    (f"{TOOLS}/drift.py", "if self.end <= self.start:", "if self.end < self.start:", D_TESTS),
    (f"{TOOLS}/drift.py", "if self.end - self.start > MAX_WINDOW:", "if self.end - self.start >= MAX_WINDOW:", D_TESTS),
    (f"{TOOLS}/drift.py", "if window.start < baseline.reference_end and", "if window.start <= baseline.reference_end and", D_TESTS),
    (f"{TOOLS}/drift.py", "elif psi >= baseline.thresholds[1]:", "elif psi > baseline.thresholds[1] * 10:", D_TESTS),
    (f"{TOOLS}/drift.py", "if len(sample.values) < baseline.min_current_count:", "if len(sample.values) <= baseline.min_current_count:", D_TESTS),
    (f"{TOOLS}/drift.py", "counts[bisect_left(", "counts[bisect_right(", D_TESTS),
    (f"{TOOLS}/drift.py", "q = max(current, baseline.smoothing)", "q = current + baseline.smoothing", D_TESTS),
    (f"{TOOLS}/drift.py", "if not 0 < warning < drift:", "if not 0 <= warning <= drift:", D_TESTS),
    (f"{TOOLS}/knowledge_search.py", "if len(hits) > arguments.top_k:", "if len(hits) > arguments.top_k + 1:", D_TESTS),
    (f"{TOOLS}/knowledge_search.py", "hit.score < arguments.min_score for hit in hits", "hit.score < 0 for hit in hits", D_TESTS),
    (f"{TOOLS}/knowledge_search.py", "Field(ge=1, le=MAX_TOP_K, strict=True)", "Field(ge=1, le=MAX_TOP_K + 1, strict=True)", D_TESTS),
    (f"{TOOLS}/knowledge_search.py", "min_length=3, max_length=1000", "min_length=2, max_length=1000", D_TESTS),
    (f"{TOOLS}/knowledge_search.py", "if any(left.score < right.score", "if any(False and left.score < right.score", D_TESTS),
    (f"{TOOLS}/runtime.py", "timeout = min(context.timeout_ms, self._max_timeout_ms)", "timeout = max(context.timeout_ms, self._max_timeout_ms)", D_TESTS),
    (f"{TOOLS}/runtime.py", "async with self._semaphore:", "if True:", D_TESTS),
    (f"{TOOLS}/runtime.py", 'return self._response(context, tool_name, "unavailable",\n                                  _error_result(exc.code, exc.public_message, retryable=exc.retryable))',
     'return self._response(context, tool_name, "error",\n                                  _error_result(exc.code, exc.public_message, retryable=exc.retryable))', D_TESTS),
    (f"{TOOLS}/fraud_scoring.py", "if (prediction.model_name, prediction.model_version) != self._model:", "if False:", D_TESTS),
    (f"{TOOLS}/fraud_scoring.py", "if prediction.transaction_id != arguments.transaction.transaction_id:", "if False:", D_TESTS),
    (f"{TOOLS}/feature_lookup.py", "if len(set(value)) != len(value):", "if False:", D_TESTS),
    (f"{TOOLS}/feature_lookup.py", "    if isinstance(value, bool):\n        raise FeatureContractError()", "    if False:\n        raise FeatureContractError()", D_TESTS),
    (f"{TOOLS}/service.py", "if asserted is not None and asserted != principal.subject:", "if False:", SEC_TESTS),
    (f"{TOOLS}/service.py", "if not accepts_json(request.headers.get(\"accept\")):", "if False:", SEC_TESTS),
    (f"{TOOLS}/service.py", "for handler in executor.handlers.values() if grants.allows(principal, handler.name)",
     "for handler in executor.handlers.values()", SEC_TESTS),
    (f"{TOOLS}/security.py", "if origin is not None and origin not in self.settings.allowed_origins:", "if False:", SEC_TESTS),
    (f"{TOOLS}/security.py", "if size > self.settings.max_body_bytes:", "if False:", SEC_TESTS),
    (f"{TOOLS}/security.py", "return tool_name in self.grants.get(principal.subject, frozenset())", "return True", SEC_TESTS),
    (f"{TOOLS}/security.py", "if self.inflight >= self.settings.max_inflight_requests:", "if False:", SEC_TESTS),
    (f"{TOOLS}/security.py", 'if message["type"] == "http.disconnect":', "if False:", SEC_TESTS),
    (f"{TOOLS}/security.py", "body = await asyncio.wait_for(self._read_body(receive), self.settings.body_read_timeout_seconds)",
     "body = await self._read_body(receive)", SEC_TESTS),
    (f"{TOOLS}/security.py", 'if parsed is None or parsed[:2] != ("application", "json"):', "if parsed is None:", SEC_TESTS),
    (f"{TOOLS}/security.py", "return best is not None and best[1] > 0", "return best is not None", SEC_TESTS),
    (f"{TOOLS}/service.py", "reason = await authenticator_readiness(authenticator)", "reason = None", SEC_TESTS),
    (f"{TOOLS}/service.py", "for name, item in report.items() if grants.allows(principal, name)",
     "for name, item in report.items()", SEC_TESTS),
    (f"{TOOLS}/service.py", '"isError": response.status != "ok",', '"isError": False,', D_TESTS),
    ("ai_platform/llm_agent/rag/pipeline.py", "if not quote or chunk.text.split()[: len(quote)] != quote:", "if not quote:", P0_TESTS),
    ("ai_platform/llm_agent/rag/pipeline.py", "if chunk is None or chunk.document_id != citation.document_id:", "if chunk is None:", P0_TESTS),
    ("ai_platform/llm_agent/rag/pipeline.py", "if seen != set(canonical.chunks):", "if False:", P0_TESTS),
    ("data_platform/orchestration/airflow/rag_params.py", "if repository_root is not None and", "if False and repository_root is not None and", P0_TESTS),
)


def main() -> int:
    interpreters = {"system": sys.argv[1], "fastapi": sys.argv[2]}
    report_path = Path(sys.argv[3])
    results = []
    with tempfile.TemporaryDirectory(prefix="mutation-smoke.") as folder:
        copy = Path(folder) / "repo"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))
        for path, original, mutated, (kind, module) in MUTANTS:
            target = copy / path
            source = target.read_text(encoding="utf-8")
            if source.count(original) != 1:
                results.append({"file": path, "mutant": mutated, "outcome": "NOT_APPLIED"})
                continue
            target.write_text(source.replace(original, mutated), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [interpreters[kind], "-m", "unittest", module], cwd=copy, capture_output=True,
                    text=True, timeout=300, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                )
                outcome = "KILLED" if proc.returncode != 0 else "SURVIVED"
            except subprocess.TimeoutExpired:
                outcome = "KILLED_TIMEOUT"
            finally:
                target.write_text(source, encoding="utf-8")
            results.append({"file": path, "mutant": mutated.splitlines()[0].strip(), "outcome": outcome})
    applied = [item for item in results if item["outcome"] != "NOT_APPLIED"]
    killed = [item for item in applied if item["outcome"].startswith("KILLED")]
    report = {
        "kind": "targeted_hand_written_mutation_smoke",
        "not_a_mutmut_campaign": True,
        "applied": len(applied),
        "killed": len(killed),
        "score": round(len(killed) / len(applied), 4) if applied else None,
        "results": results,
    }
    report_path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("applied", "killed", "score")}))
    return 0 if len(applied) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
