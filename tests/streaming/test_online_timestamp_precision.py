"""Online rows keep Feast's stored microseconds for the direct Offline comparison."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.verification.consistency import online_rows_from_feast

VIEW = "account_behavior"
SPEC = FEATURE_SPECS[VIEW]
EXACT = datetime(2026, 6, 1, 1, 10, 1, 370000, tzinfo=timezone.utc)


class FakeStore:
    """get_online_features reports whole seconds; online_read keeps microseconds."""

    def __init__(self, exact, whole):
        self.exact, self.whole = exact, whole
        self.config = SimpleNamespace()
        self.read_keys = None

    def get_online_features(self, features, entity_rows, full_feature_names):
        result = {f"{VIEW}__{feature}": [1.0] * len(entity_rows) for feature in SPEC["features"]}
        result[f"{VIEW}__{SPEC['features'][0]}__ts"] = [self.whole] * len(entity_rows)
        return SimpleNamespace(to_dict=lambda include_event_timestamps: result)

    def get_feature_view(self, name):
        return SimpleNamespace(name=name)

    def _get_provider(self):
        store = self

        class Provider:
            def online_read(self, config, table, entity_keys, requested_features):
                store.read_keys = (table.name, entity_keys, requested_features)
                return [(store.exact, {}) for _ in entity_keys]

        return Provider()


class OnlineTimestampPrecisionTest(unittest.TestCase):
    def test_row_carries_the_stored_microseconds(self):
        store = FakeStore(EXACT, int(EXACT.timestamp()))
        rows = online_rows_from_feast(store, [(VIEW, "acct-1")])
        self.assertEqual(rows[(VIEW, "acct-1")]["__ts"], EXACT)
        table, keys, requested = store.read_keys
        self.assertEqual(table, VIEW)
        self.assertEqual(list(keys[0].join_keys), [SPEC["entity_key"]])
        self.assertEqual(keys[0].entity_values[0].string_val, "acct-1")
        self.assertEqual(requested, list(SPEC["features"]))

    def test_reads_that_disagree_on_the_second_fail_closed(self):
        store = FakeStore(EXACT, int(EXACT.timestamp()) + 1)
        with self.assertRaisesRegex(ValueError, "disagree"):
            online_rows_from_feast(store, [(VIEW, "acct-1")])

    def test_one_read_missing_a_row_fails_closed(self):
        for exact, whole in ((None, int(EXACT.timestamp())), (EXACT, None)):
            with self.subTest(exact=exact, whole=whole), self.assertRaisesRegex(ValueError, "disagree"):
                online_rows_from_feast(FakeStore(exact, whole), [(VIEW, "acct-1")])

    def test_entity_absent_from_both_reads_has_no_timestamp(self):
        rows = online_rows_from_feast(FakeStore(None, None), [(VIEW, "acct-1")])
        self.assertIsNone(rows[(VIEW, "acct-1")]["__ts"])


if __name__ == "__main__":
    unittest.main()
