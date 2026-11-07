"""Schema diagrams stay generated from real schemas and logical contracts."""

from __future__ import annotations

import ast
import copy
import json
import re
import unittest
from pathlib import Path

import yaml

from ops.schema.export_observed_schemas import export_zone, parse_delta_metadata
from ops.schema.render_schema_diagrams import (
    RELATIONSHIPS,
    SQL_TYPES,
    column_changes,
    gold_role,
    load_relationships,
    render_all,
    render_dbml,
    render_erd_sql,
    render_mermaid,
    validate_relationships,
)


ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "docs/edai1/schema/generated"


def inputs():
    observed = json.loads((GENERATED / "observed_schemas.json").read_text(encoding="utf-8"))
    return observed, yaml.safe_load((ROOT / RELATIONSHIPS).read_text(encoding="utf-8"))


class CommittedArtifactsTest(unittest.TestCase):
    def test_committed_files_match_their_inputs(self):
        for name, content in render_all(*inputs()).items():
            with self.subTest(name):
                self.assertEqual(content, (GENERATED / name).read_text(encoding="utf-8"))

    def test_the_inventory_matches_the_canonical_table_counts(self):
        observed, _ = inputs()
        counts = {zone: len(value["tables"]) for zone, value in observed["zones"].items()}
        self.assertEqual({"bronze": 8, "silver": 8, "gold": 11, "features": 4}, counts)

    def test_the_inventory_holds_metadata_only(self):
        observed, _ = inputs()
        for zone in observed["zones"].values():
            for table in zone["tables"].values():
                self.assertEqual({"columns", "partition_columns"}, set(table))
                for column in table["columns"]:
                    self.assertEqual({"name", "type", "nullable"}, set(column))

    def test_every_observed_type_has_an_erd_type(self):
        observed, _ = inputs()
        for zone in observed["zones"].values():
            for table in zone["tables"].values():
                for column in table["columns"]:
                    self.assertIn(column["type"], SQL_TYPES)


class RelationshipContractTest(unittest.TestCase):
    def test_every_key_and_relationship_exists_in_the_real_schema(self):
        self.assertEqual([], validate_relationships(*inputs()))

    def test_a_missing_column_is_reported(self):
        observed, document = inputs()
        document = copy.deepcopy(document)
        document["relationships"]["gold"][0]["column"] = "no_such_column"
        self.assertTrue(validate_relationships(observed, document))
        with self.assertRaises(ValueError):
            render_all(observed, document)

    def test_a_missing_key_is_reported(self):
        observed, document = inputs()
        document = copy.deepcopy(document)
        document["keys"]["gold"]["dim_user"] = ["no_such_column"]
        self.assertTrue(validate_relationships(observed, document))

    def test_every_silver_and_gold_table_has_a_key(self):
        observed, document = inputs()
        for zone in ("silver", "gold", "features"):
            self.assertEqual(set(observed["zones"][zone]["tables"]), set(document["keys"][zone]))

    def test_gold_keys_match_the_spark_summary_contract(self):
        from ops.gcp.summarize_spark_output import GOLD_PRIMARY_KEYS

        _, document = inputs()
        self.assertEqual({key: list(value) for key, value in GOLD_PRIMARY_KEYS.items()}, document["keys"]["gold"])

    def test_enforcement_labels_match_the_validators(self):
        rules = set()
        for path in ("data_platform/quality/validate_gold.py", "data_platform/quality/validate_silver.py"):
            # The AST joins adjacent string literals that a text search would miss.
            for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"[a-z_0-9]+\.[a-z_]+_exists_in_[a-z_]+", node.value):
                        rules.add(node.value)
        _, document = inputs()
        for item in load_relationships(document):
            rule = f"{item.table}.{item.column}_exists_in_{item.target_table}"
            with self.subTest(rule):
                self.assertEqual("validated" if rule in rules else "builder_only", item.enforcement)

    def test_merchant_and_counterparty_stay_nullable(self):
        _, document = inputs()
        nullable = {(item.table, item.column) for item in load_relationships(document)
                    if item.zone == "gold" and item.nullable}
        self.assertEqual({("fact_transactions", "merchant_id"),
                          ("fact_transactions", "counterparty_account_id")}, nullable)

    def test_feature_tables_reference_gold_dimensions_across_zones(self):
        _, document = inputs()
        links = [item for item in load_relationships(document) if item.zone == "features"]
        self.assertEqual({"gold"}, {item.target_zone for item in links})


class RenderingTest(unittest.TestCase):
    def setUp(self):
        self.observed, self.document = inputs()

    def test_mermaid_marks_keys_and_optional_references(self):
        text = render_mermaid("gold", self.observed, self.document)
        self.assertIn("string transaction_id PK", text)
        self.assertIn("string user_id FK", text)
        self.assertIn("DIM_MERCHANT o|--o{ FACT_TRANSACTIONS : merchant_id", text)
        self.assertIn("DIM_USER ||--o{ FACT_TRANSACTIONS : user_id", text)

    def test_bronze_declares_no_keys(self):
        text = render_mermaid("bronze", self.observed, self.document)
        self.assertNotIn(" PK", text)
        self.assertNotIn(" FK", text)

    def test_dbml_carries_composite_keys_and_references(self):
        text = render_dbml(("gold", "features"), self.observed, self.document)
        self.assertIn("(account_id, snapshot_date) [pk]", text)
        self.assertIn("Ref: gold.fact_transactions.user_id > gold.dim_user.user_id", text)
        self.assertIn("Ref: features.feat_user_behavior.user_id > gold.dim_user.user_id", text)

    def test_the_erd_sql_says_it_is_logical(self):
        text = render_erd_sql(("gold", "features"), self.observed, self.document)
        self.assertIn("ERD import only", text)
        self.assertIn("do not exist in", text)
        self.assertIn('REFERENCES gold.dim_user ("user_id")', text)

    def test_output_is_deterministic(self):
        self.assertEqual(render_all(self.observed, self.document), render_all(self.observed, self.document))

    def test_roles_follow_the_naming_convention(self):
        self.assertEqual("Fact", gold_role("fact_transactions"))
        self.assertEqual("One big table", gold_role("obt_transaction_enriched"))
        self.assertEqual("Analytics aggregate", gold_role("opt_merchant_performance"))
        self.assertEqual("Other", gold_role("unknown"))

    def test_real_bronze_to_silver_changes_are_reported(self):
        changes = {row[0]: row for row in column_changes(
            self.observed["zones"]["bronze"], self.observed["zones"]["silver"])}
        self.assertEqual(["event_date"], changes["transactions"][1])
        self.assertEqual(["login_date"], changes["login_events"][1])
        self.assertEqual([], changes["users"][1])


class DeltaMetadataTest(unittest.TestCase):
    COMMIT = "\n".join((
        json.dumps({"protocol": {"minReaderVersion": 1}}),
        json.dumps({"metaData": {
            "schemaString": json.dumps({"type": "struct", "fields": [
                {"name": "a", "type": "string", "nullable": True, "metadata": {}}]}),
            "partitionColumns": ["a"],
        }}),
    ))

    def test_columns_and_partitions_are_read(self):
        self.assertEqual(
            {"columns": [{"name": "a", "type": "string", "nullable": True}], "partition_columns": ["a"]},
            parse_delta_metadata(self.COMMIT),
        )

    def test_a_commit_without_metadata_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_delta_metadata('{"protocol": {}}')

    def test_a_zone_export_reads_only_the_first_commit_of_each_table(self):
        reads = []

        def read(uri):
            reads.append(uri)
            return self.COMMIT

        result = export_zone("gs://b/root", "run", lambda _: ["t1"], read)
        self.assertEqual(["gs://b/root/t1/_delta_log/00000000000000000000.json"], reads)
        self.assertEqual("run", result["run_id"])


if __name__ == "__main__":
    unittest.main()
