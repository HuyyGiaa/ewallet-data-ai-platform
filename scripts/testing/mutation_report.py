"""Summarize ``mutmut results --all true`` output for B1.3 evidence.

The report keeps killed, survived, timeout, error and reviewed-equivalent
mutants apart and states two scores, so a reader can see what the equivalence
review changed. Equivalents are read from a reviewed list, never inferred.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path


KILLED = "killed"
SURVIVED = "survived"
TIMEOUT = "timeout"
NO_TESTS = "no tests"
SKIPPED = "skipped"


@dataclass(frozen=True)
class MutationSummary:
    """Mutant totals after separating reviewed equivalents from survivors."""

    total: int
    killed: int
    survived: int
    equivalent: int
    timeout: int
    no_tests: int
    skipped: int
    errors: int
    stale_equivalents: tuple[str, ...]
    by_module: dict[str, dict[str, int]]

    @property
    def raw_score(self) -> float | None:
        """Killed share of every mutant that was exercised."""
        return _ratio(self.killed, self.total - self.skipped)

    @property
    def actionable_score(self) -> float | None:
        """Killed share after removing reviewed equivalent mutants."""
        return _ratio(self.killed, self.total - self.skipped - self.equivalent)

    def as_dict(self) -> dict[str, object]:
        """Return the JSON form of the summary."""
        return {
            "total": self.total,
            "killed": self.killed,
            "survived_actionable": self.survived,
            "equivalent_reviewed": self.equivalent,
            "timeout": self.timeout,
            "no_tests": self.no_tests,
            "skipped": self.skipped,
            "errors": self.errors,
            "raw_score": self.raw_score,
            "actionable_score": self.actionable_score,
            "stale_equivalents": list(self.stale_equivalents),
            "by_module": self.by_module,
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def parse_results(text: str) -> dict[str, str]:
    """Parse ``name: status`` lines, ignoring every other line."""
    results: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, status = line.strip().rpartition(": ")
        if separator and "__mutmut_" in name:
            results[name] = status.strip()
    return results


def parse_equivalents(text: str) -> frozenset[str]:
    """Read mutant names, ignoring comments, blanks and trailing reasons."""
    names = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return frozenset(name for name in names if name)


def module_of(mutant_name: str) -> str:
    """Return the dotted module owning a mutant."""
    prefix = mutant_name.split("__mutmut_", 1)[0]
    parts = prefix.split(".")
    while parts and not parts[-1].startswith(("x_", "xǁ")):
        parts.pop()
    return ".".join(parts[:-1]) if parts else prefix


def summarize(results: dict[str, str], equivalents: frozenset[str]) -> MutationSummary:
    """Classify every mutant; only a survivor can count as equivalent."""
    counts: Counter[str] = Counter()
    by_module: dict[str, Counter[str]] = {}
    for name, status in results.items():
        if status == SURVIVED and name in equivalents:
            category = "equivalent"
        elif status in {KILLED, SURVIVED, TIMEOUT, NO_TESTS, SKIPPED}:
            category = status
        else:
            category = "errors"
        counts[category] += 1
        by_module.setdefault(module_of(name), Counter())[category] += 1

    stale = tuple(
        sorted(name for name in equivalents if results.get(name) != SURVIVED)
    )
    return MutationSummary(
        total=len(results),
        killed=counts[KILLED],
        survived=counts[SURVIVED],
        equivalent=counts["equivalent"],
        timeout=counts[TIMEOUT],
        no_tests=counts[NO_TESTS],
        skipped=counts[SKIPPED],
        errors=counts["errors"],
        stale_equivalents=stale,
        by_module={name: dict(value) for name, value in sorted(by_module.items())},
    )


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def render_markdown(summary: MutationSummary) -> str:
    """Render the summary as a Markdown table."""
    lines = [
        "# Mutation summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total mutants | {summary.total} |",
        f"| Killed | {summary.killed} |",
        f"| Survived (actionable) | {summary.survived} |",
        f"| Survived (reviewed equivalent) | {summary.equivalent} |",
        f"| Timeout | {summary.timeout} |",
        f"| No covering test | {summary.no_tests} |",
        f"| Skipped | {summary.skipped} |",
        f"| Error or other status | {summary.errors} |",
        f"| Raw score | {_percent(summary.raw_score)} |",
        f"| Actionable score | {_percent(summary.actionable_score)} |",
        "",
        "| Module | killed | survived | equivalent | timeout | no tests | errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for module, counts in summary.by_module.items():
        cells = (
            counts.get(key, 0)
            for key in (KILLED, SURVIVED, "equivalent", TIMEOUT, NO_TESTS, "errors")
        )
        lines.append(f"| {module} | " + " | ".join(map(str, cells)) + " |")
    if summary.stale_equivalents:
        lines += ["", "Equivalence entries that no longer match a survivor:", ""]
        lines += [f"- `{name}`" for name in summary.stale_equivalents]
    return "\n".join(lines) + "\n"


def main() -> None:
    """Write ``<output>.json`` and ``<output>.md`` and print the Markdown."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--equivalents", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()

    summary = summarize(
        parse_results(options.results.read_text(encoding="utf-8")),
        parse_equivalents(options.equivalents.read_text(encoding="utf-8")),
    )
    markdown = render_markdown(summary)
    options.output.with_suffix(".json").write_text(
        json.dumps(summary.as_dict(), indent=2) + "\n", encoding="utf-8"
    )
    options.output.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
