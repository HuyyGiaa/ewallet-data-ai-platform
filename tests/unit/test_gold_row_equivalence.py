"""Regression, boundary and report tests for the Gold row-level comparison."""

from __future__ import annotations

import math
import unittest

import pyarrow as pa

from data_platform.quality.gold_equivalence import (
    FINAL_VERDICT_SCHEMA,
    GOLD_POLICIES,
    ArrowSource,
    FloatTolerance,
    TablePolicy,
    compare_table,
    digest,
    final_verdict,
    row_report,
)


TOLERANCE = FloatTolerance(1e-12, 0.0, "test")
POLICY = TablePolicy(keys=("id",), float_tolerances={"amount": TOLERANCE})


def table(ids=("a", "b", "c"), amount=(1.0, 2.0, 3.0), other=(1.5, 2.5, 3.5), count=(1, 2, 3), label=("x", "y", "z")):
    return pa.table(
        {
            "id": pa.array(ids, pa.string()),
            "amount": pa.array(amount, pa.float64()),
            "other": pa.array(other, pa.float64()),
            "count": pa.array(count, pa.int64()),
            "label": pa.array(label, pa.string()),
        }
    )


def compare(left, right, policy=POLICY):
    return compare_table("t", ArrowSource(left), ArrowSource(right), policy)


def next_up(value: float, steps: int = 1) -> float:
    for _ in range(steps):
        value = math.nextafter(value, math.inf)
    return value


class EquivalentOutputTest(unittest.TestCase):
    def test_identical_tables_pass(self):
        self.assertTrue(compare(table(), table())["passed"])

    def test_row_order_does_not_matter(self):
        shuffled = table(("c", "a", "b"), (3.0, 1.0, 2.0), (3.5, 1.5, 2.5), (3, 1, 2), ("z", "x", "y"))
        self.assertTrue(compare(table(), shuffled)["passed"])

    def test_a_last_bit_difference_in_an_allowlisted_column_passes(self):
        report = compare(table(), table(amount=(next_up(1.0), 2.0, 3.0)))
        column = report["columns"]["amount"]
        self.assertTrue(report["passed"])
        self.assertEqual(1, column["exact_mismatch_rows"])
        self.assertEqual(0, column["over_tolerance_rows"])
        self.assertEqual(1, column["difference"]["ulp_max"])

    def test_values_on_two_sides_of_a_rounding_boundary_pass(self):
        """The case a rounded fingerprint gets wrong."""
        low, high = 1.0000000000049, 1.0000000000051
        self.assertNotEqual("%.12g" % low, "%.12g" % high)
        self.assertTrue(compare(table(amount=(low, 2.0, 3.0)), table(amount=(high, 2.0, 3.0)))["passed"])

    def test_two_nulls_are_equal(self):
        values = (None, 2.0, 3.0)
        self.assertTrue(compare(table(amount=values), table(amount=values))["passed"])

    def test_two_nans_are_equal(self):
        values = (math.nan, 2.0, 3.0)
        self.assertTrue(compare(table(amount=values), table(amount=values))["passed"])

    def test_equal_infinities_are_equal(self):
        values = (math.inf, 2.0, 3.0)
        self.assertTrue(compare(table(amount=values), table(amount=values))["passed"])


class ToleranceBoundaryTest(unittest.TestCase):
    def passes(self, left, right):
        return compare(table(amount=(left, 2.0, 3.0)), table(amount=(right, 2.0, 3.0)))["passed"]

    def test_inside_the_tolerance(self):
        self.assertTrue(self.passes(1000.0, 1000.0 + 9e-10))

    def test_just_outside_the_tolerance(self):
        self.assertFalse(self.passes(1000.0, 1000.0 + 2e-9))

    def test_the_tolerance_scales_with_the_value(self):
        self.assertTrue(self.passes(1e9, 1e9 + 5e-4))
        self.assertFalse(self.passes(1e3, 1e3 + 5e-4))

    def test_zero_only_matches_zero_without_an_absolute_tolerance(self):
        self.assertFalse(self.passes(0.0, 1e-300))

    def test_an_absolute_tolerance_is_honoured(self):
        policy = TablePolicy(("id",), float_tolerances={"amount": FloatTolerance(0.0, 1e-6, "test")})
        report = compare(table(amount=(0.0, 2.0, 3.0)), table(amount=(1e-7, 2.0, 3.0)), policy)
        self.assertTrue(report["passed"])

    def test_opposite_signs_of_a_large_value_fail(self):
        self.assertFalse(self.passes(5.0, -5.0))

    def test_a_loose_tolerance_cannot_be_declared(self):
        with self.assertRaises(ValueError):
            FloatTolerance(0.01, 0.0, "too loose")

    def test_a_tolerance_needs_a_reason(self):
        with self.assertRaises(ValueError):
            FloatTolerance(1e-12, 0.0, "")


class SemanticFailureTest(unittest.TestCase):
    def test_a_float_outside_the_allowlist_must_match_every_bit(self):
        report = compare(table(), table(other=(next_up(1.5), 2.5, 3.5)))
        self.assertFalse(report["passed"])
        self.assertEqual("exact", report["columns"]["other"]["comparison"])
        self.assertEqual(1, report["columns"]["other"]["over_tolerance_rows"])

    def test_signed_zero_differs_in_an_exact_float_column(self):
        self.assertFalse(compare(table(other=(0.0, 2.5, 3.5)), table(other=(-0.0, 2.5, 3.5)))["passed"])

    def test_an_integer_difference_fails(self):
        report = compare(table(), table(count=(1, 2, 4)))
        self.assertFalse(report["passed"])
        self.assertEqual(1, report["columns"]["count"]["mismatch_rows"])

    def test_a_string_difference_fails(self):
        self.assertFalse(compare(table(), table(label=("x", "y", "Z")))["passed"])

    def test_null_against_a_value_fails_in_every_kind_of_column(self):
        self.assertFalse(compare(table(amount=(None, 2.0, 3.0)), table())["passed"])
        self.assertFalse(compare(table(), table(amount=(None, 2.0, 3.0)))["passed"])
        self.assertFalse(compare(table(count=(None, 2, 3)), table())["passed"])
        self.assertFalse(compare(table(label=(None, "y", "z")), table())["passed"])

    def test_nan_against_a_value_fails_even_when_allowlisted(self):
        report = compare(table(amount=(math.nan, 2.0, 3.0)), table())
        self.assertFalse(report["passed"])
        self.assertEqual(1, report["columns"]["amount"]["non_finite_mismatch_rows"])

    def test_infinity_against_a_value_fails_even_when_allowlisted(self):
        self.assertFalse(compare(table(amount=(math.inf, 2.0, 3.0)), table())["passed"])

    def test_opposite_infinities_fail(self):
        self.assertFalse(compare(table(amount=(math.inf, 2.0, 3.0)), table(amount=(-math.inf, 2.0, 3.0)))["passed"])

    def test_a_missing_row_fails(self):
        smaller = table(("a", "b"), (1.0, 2.0), (1.5, 2.5), (1, 2), ("x", "y"))
        report = compare(table(), smaller)
        self.assertFalse(report["passed"])
        self.assertEqual({"baseline": 3, "optimized": 2}, report["row_counts"])

    def test_a_different_key_fails(self):
        report = compare(table(), table(ids=("a", "b", "d")))
        self.assertFalse(report["passed"])
        self.assertFalse(report["keys_report"]["keys_identical"])

    def test_duplicate_keys_fail_even_on_identical_tables(self):
        duplicated = table(ids=("a", "a", "c"))
        report = compare(duplicated, duplicated)
        self.assertFalse(report["passed"])
        self.assertEqual({"baseline": 1, "optimized": 1}, report["keys_report"]["duplicate_keys"])

    def test_null_keys_fail(self):
        nulled = table(ids=(None, "b", "c"))
        self.assertFalse(compare(nulled, nulled)["passed"])

    def test_a_schema_difference_stops_the_comparison(self):
        wider = table().append_column("extra", pa.array([1, 2, 3]))
        report = compare(table(), wider)
        self.assertFalse(report["passed"])
        self.assertFalse(report["schema_identical"])

    def test_a_type_change_is_a_schema_difference(self):
        changed = table().set_column(3, "count", pa.array([1.0, 2.0, 3.0]))
        self.assertFalse(compare(table(), changed)["schema_identical"])

    def test_a_tolerance_on_a_non_float_column_is_refused(self):
        policy = TablePolicy(("id",), float_tolerances={"count": TOLERANCE})
        with self.assertRaisesRegex(ValueError, "not a float"):
            compare(table(), table(), policy)


class CompositeKeyTest(unittest.TestCase):
    POLICY = TablePolicy(keys=("a", "b"))

    def frame(self, first, second, value):
        return pa.table({"a": first, "b": second, "v": pa.array(value, pa.int64())})

    def test_rows_align_on_all_key_columns(self):
        left = self.frame(["x", "x"], [1, 2], [10, 20])
        right = self.frame(["x", "x"], [2, 1], [20, 10])
        self.assertTrue(compare(left, right, self.POLICY)["passed"])

    def test_a_repeat_of_one_key_column_is_not_a_duplicate(self):
        left = self.frame(["x", "x"], [1, 2], [1, 2])
        self.assertEqual(0, compare(left, left, self.POLICY)["keys_report"]["duplicate_keys"]["baseline"])

    def test_a_repeat_of_the_whole_key_is_a_duplicate(self):
        left = self.frame(["x", "x"], [1, 1], [1, 2])
        self.assertFalse(compare(left, left, self.POLICY)["passed"])


class VolatileColumnTest(unittest.TestCase):
    POLICY = TablePolicy(("id",), volatile_columns=("stamp",))

    def frame(self, stamps):
        return pa.table({"id": ["a", "b"], "stamp": pa.array(stamps, pa.int64())})

    def test_write_time_values_may_differ(self):
        self.assertTrue(compare(self.frame([1, 2]), self.frame([3, 4]), self.POLICY)["passed"])

    def test_a_null_audit_value_fails(self):
        self.assertFalse(compare(self.frame([None, 2]), self.frame([3, 4]), self.POLICY)["passed"])


class PolicyContractTest(unittest.TestCase):
    def test_exactly_the_three_diagnosed_columns_are_allowlisted(self):
        allowed = {
            f"{name}.{column}"
            for name, policy in GOLD_POLICIES.items()
            for column in policy.float_tolerances
        }
        self.assertEqual(
            {
                "feat_user_90d.f_user_avg_transaction_amount_90d",
                "opt_merchant_performance.total_amount",
                "opt_merchant_performance.avg_amount",
            },
            allowed,
        )

    def test_every_gold_table_has_a_policy(self):
        self.assertEqual(11, len(GOLD_POLICIES))

    def test_failed_rate_stays_exact(self):
        self.assertNotIn(
            "f_user_failed_transaction_rate_90d", GOLD_POLICIES["feat_user_90d"].float_tolerances
        )


def merchant_table(**overrides):
    values = dict(
        merchant_id=["m1", "m2"], transaction_count=[4, 2], total_amount=[40.0, 30.0],
        avg_amount=[10.0, 15.0], success_count=[3, 2], failed_count=[1, 0], distinct_users=[2, 1],
    )
    values.update(overrides)
    return pa.table(values)


class InvariantTest(unittest.TestCase):
    POLICY = GOLD_POLICIES["opt_merchant_performance"]

    def check(self, **overrides):
        data = merchant_table(**overrides)
        return compare_table("opt_merchant_performance", ArrowSource(data), ArrowSource(data), self.POLICY)

    def test_consistent_rows_pass(self):
        self.assertTrue(self.check()["passed"])

    def test_identical_outputs_can_still_break_a_business_rule(self):
        report = self.check(avg_amount=[11.0, 15.0])
        self.assertFalse(report["passed"])
        self.assertEqual(1, report["invariant_violations"]["baseline"]["average_is_total_over_count"])

    def test_outcomes_cannot_exceed_transactions(self):
        self.assertFalse(self.check(success_count=[9, 2])["passed"])

    def test_distinct_users_cannot_exceed_transactions(self):
        self.assertFalse(self.check(distinct_users=[9, 1])["passed"])

    def test_user_failed_rate_must_be_a_rate(self):
        data = pa.table({
            "user_id": ["u"], "f_user_total_transactions_90d": [1],
            "f_user_avg_transaction_amount_90d": [1.0], "f_user_failed_transaction_rate_90d": [1.5],
            "f_user_distinct_merchants_90d": [1],
        })
        report = compare_table("feat_user_90d", ArrowSource(data), ArrowSource(data), GOLD_POLICIES["feat_user_90d"])
        self.assertFalse(report["passed"])


class ReportTest(unittest.TestCase):
    def report(self, *tables):
        return row_report(list(tables), {"baseline_root": "b"})

    def test_worst_examples_name_the_row(self):
        column = compare(table(), table(amount=(next_up(1.0), 2.0, 3.0)))["columns"]["amount"]
        self.assertEqual({"id": "a"}, column["worst_examples"][0]["key"])

    def test_an_empty_report_cannot_pass(self):
        self.assertEqual("FAIL", self.report()["status"])

    def test_one_failed_table_fails_the_report(self):
        good, bad = compare(table(), table()), compare(table(), table(count=(9, 2, 3)))
        self.assertEqual("FAIL", self.report(good, bad)["status"])

    def test_the_digest_covers_the_content(self):
        report = self.report(compare(table(), table()))
        self.assertEqual(report["report_digest"], digest(report, "report_digest"))
        edited = {**report, "status": "FAIL"}
        self.assertNotEqual(report["report_digest"], digest(edited, "report_digest"))


class FinalVerdictTest(unittest.TestCase):
    def rows(self, passed=True, name="feat_user_90d"):
        item = compare(table(), table(count=(1, 2, 3) if passed else (9, 2, 3)))
        return row_report([{**item, "table": name}], {})

    EXACT = {
        "output_equivalent": False,
        "differences": ["feat_user_90d.semantic_content_fingerprint"],
        "equivalence_artifact_digest": "sha256:x",
    }

    def test_a_passing_row_report_resolves_the_fingerprint(self):
        verdict = final_verdict(self.EXACT, self.rows())
        self.assertEqual("TOLERANCE_EQUIVALENT", verdict["status"])
        self.assertEqual(FINAL_VERDICT_SCHEMA, verdict["schema_version"])

    def test_the_failed_exact_result_stays_visible(self):
        verdict = final_verdict(self.EXACT, self.rows())
        self.assertFalse(verdict["exact_equivalent"])
        self.assertEqual(self.EXACT["differences"], verdict["exact_differences"])

    def test_the_verdict_adopts_nothing(self):
        self.assertIn("owner decision", final_verdict(self.EXACT, self.rows())["adoption"])

    def test_a_failing_row_report_is_not_equivalent(self):
        self.assertEqual("NOT_EQUIVALENT", final_verdict(self.EXACT, self.rows(False))["status"])

    def test_a_table_outside_the_row_report_stays_unresolved(self):
        verdict = final_verdict(self.EXACT, self.rows(name="opt_merchant_performance"))
        self.assertEqual("NOT_EQUIVALENT", verdict["status"])
        self.assertEqual(self.EXACT["differences"], verdict["unresolved_differences"])

    def test_other_exact_differences_are_never_resolved(self):
        for difference in ("feat_user_90d.row_count", "feat_user_90d.schema_sha256",
                           "baseline.invariant.business_invariants"):
            exact = {**self.EXACT, "differences": [*self.EXACT["differences"], difference]}
            with self.subTest(difference):
                self.assertEqual("NOT_EQUIVALENT", final_verdict(exact, self.rows())["status"])

    def test_an_exact_pass_stays_exact(self):
        exact = {"output_equivalent": True, "differences": []}
        self.assertEqual("EXACT_EQUIVALENT", final_verdict(exact, self.rows())["status"])

    def test_an_edited_row_report_is_refused(self):
        report = {**self.rows(False), "status": "PASS"}
        with self.assertRaisesRegex(ValueError, "digest"):
            final_verdict(self.EXACT, report)


if __name__ == "__main__":
    unittest.main()
