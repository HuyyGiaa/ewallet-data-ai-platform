"""Classification rules of the mutation evidence report."""

from __future__ import annotations

import unittest

from scripts.testing.mutation_report import (
    module_of,
    parse_equivalents,
    parse_results,
    render_markdown,
    summarize,
)


KILLED = "pkg.mod.x_run__mutmut_1"
SURVIVED = "pkg.mod.x_run__mutmut_2"
EQUIVALENT = "pkg.mod.x_run__mutmut_3"
TIMED_OUT = "pkg.other.x_go__mutmut_1"
UNCOVERED = "pkg.other.x_go__mutmut_2"
CRASHED = "pkg.other.x_go__mutmut_3"
RESULTS = f"""
progress noise
    {KILLED}: killed
    {SURVIVED}: survived
    {EQUIVALENT}: survived
    {TIMED_OUT}: timeout
    {UNCOVERED}: no tests
    {CRASHED}: segfault
"""


class ParsingTest(unittest.TestCase):
    def test_only_mutant_lines_are_read(self):
        self.assertEqual(6, len(parse_results(RESULTS)))

    def test_equivalent_list_ignores_comments_and_reasons(self):
        names = parse_equivalents(f"# header\n\n{EQUIVALENT}  # same value\n")
        self.assertEqual(frozenset({EQUIVALENT}), names)

    def test_module_is_derived_from_the_mutant_name(self):
        self.assertEqual("pkg.mod", module_of(KILLED))
        self.assertEqual("pkg.mod", module_of("pkg.mod.xǁClassǁrun__mutmut_4"))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.summary = summarize(
            parse_results(RESULTS), frozenset({EQUIVALENT, "pkg.mod.x_gone__mutmut_9"})
        )

    def test_every_status_stays_in_its_own_category(self):
        self.assertEqual(
            (6, 1, 1, 1, 1, 1, 1),
            (
                self.summary.total,
                self.summary.killed,
                self.summary.survived,
                self.summary.equivalent,
                self.summary.timeout,
                self.summary.no_tests,
                self.summary.errors,
            ),
        )

    def test_equivalence_only_shrinks_the_actionable_denominator(self):
        self.assertAlmostEqual(1 / 6, self.summary.raw_score)
        self.assertAlmostEqual(1 / 5, self.summary.actionable_score)

    def test_a_killed_mutant_cannot_be_declared_equivalent(self):
        summary = summarize(parse_results(RESULTS), frozenset({KILLED}))
        self.assertEqual(1, summary.killed)
        self.assertEqual(0, summary.equivalent)
        self.assertEqual((KILLED,), summary.stale_equivalents)

    def test_stale_entries_are_reported(self):
        self.assertEqual(("pkg.mod.x_gone__mutmut_9",), self.summary.stale_equivalents)
        self.assertIn("no longer match", render_markdown(self.summary))

    def test_an_empty_run_has_no_score(self):
        self.assertIsNone(summarize({}, frozenset()).raw_score)


if __name__ == "__main__":
    unittest.main()
