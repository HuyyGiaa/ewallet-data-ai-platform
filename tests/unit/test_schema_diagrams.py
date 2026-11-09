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
    Evidence,
    cloud_reference_checks,
    edge_semantics,
    load_evidence,
    validator_edges,
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
        self.assertIn("Ref fk_fact_transactions_user_id: gold.fact_transactions.user_id > gold.dim_user.user_id", text)
        self.assertIn("Ref fk_feat_user_behavior_user_id: features.feat_user_behavior.user_id > gold.dim_user.user_id",
                      text)

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


REFS = re.compile(r"^Ref fk_(\w+): (\S+) (>\?|>|-\?|-) (\S+)$", re.MULTILINE)


def dbml_refs(text):
    return {name: (left, operator, right) for name, left, operator, right in REFS.findall(text)}


class DbmlSemanticsTest(unittest.TestCase):
    """The DBML says what the contract says, in DBML's own operator semantics."""

    def setUp(self):
        self.observed, self.document = inputs()
        self.silver = render_dbml(("silver",), self.observed, self.document)
        self.gold = render_dbml(("gold", "features"), self.observed, self.document)

    def test_silver_has_eight_tables_and_eleven_references(self):
        self.assertEqual(8, len(re.findall(r"^Table silver\.", self.silver, re.MULTILINE)))
        self.assertEqual(11, len(dbml_refs(self.silver)))

    def test_gold_has_fifteen_tables_and_twenty_references(self):
        self.assertEqual(15, len(re.findall(r"^Table (gold|features)\.", self.gold, re.MULTILINE)))
        self.assertEqual(20, len(dbml_refs(self.gold)))

    def test_required_references_are_not_null_many_to_one(self):
        refs = dbml_refs(self.silver)
        self.assertEqual(("silver.accounts.user_id", ">", "silver.users.user_id"), refs["accounts_user_id"])
        self.assertIn("  user_id string [not null, note:", self.silver)
        refs = dbml_refs(self.gold)
        self.assertEqual(("gold.fact_transactions.user_id", ">", "gold.dim_user.user_id"),
                         refs["fact_transactions_user_id"])

    def test_conditional_merchant_and_counterparty_stay_optional(self):
        for text, zone, table, parent in ((self.silver, "silver", "transactions", ("merchants", "accounts")),
                                          (self.gold, "gold", "fact_transactions", ("dim_merchant", "dim_account"))):
            refs = dbml_refs(text)
            self.assertEqual(">?", refs[f"{table}_merchant_id"][1])
            self.assertEqual(">?", refs[f"{table}_counterparty_account_id"][1])
            self.assertEqual(f"{zone}.{parent[0]}.merchant_id", refs[f"{table}_merchant_id"][2])
            self.assertEqual(f"{zone}.{parent[1]}.account_id", refs[f"{table}_counterparty_account_id"][2])
            self.assertIn("  merchant_id string [null, note:", text)
            self.assertIn("  counterparty_account_id string [null, note:", text)

    def test_one_to_one_edges_put_the_parent_first(self):
        refs = dbml_refs(self.gold)
        self.assertEqual(("gold.dim_user.user_id", "-", "gold.feat_user_90d.user_id"), refs["feat_user_90d_user_id"])
        self.assertEqual(("gold.dim_merchant.merchant_id", "-", "gold.opt_merchant_performance.merchant_id"),
                         refs["opt_merchant_performance_merchant_id"])
        self.assertEqual(("gold.fact_transactions.transaction_id", "-", "gold.obt_transaction_enriched.transaction_id"),
                         refs["obt_transaction_enriched_transaction_id"])
        self.assertEqual(("silver.transactions.transaction_id", "-", "silver.fraud_labels.transaction_id"),
                         dbml_refs(self.silver)["fraud_labels_transaction_id"])

    def test_a_unique_child_without_complete_coverage_is_optional_one_to_one(self):
        document = copy.deepcopy(self.document)
        for item in document["relationships"]["gold"]:
            if item["table"] == "feat_user_90d":
                del item["coverage"]
        refs = dbml_refs(render_dbml(("gold", "features"), self.observed, document))
        self.assertEqual("-?", refs["feat_user_90d_user_id"][1])

    def test_composite_keys_stay_composite_and_their_fk_column_many(self):
        self.assertIn("(account_id, snapshot_date) [pk]", self.gold)
        self.assertIn("(user_id, event_timestamp) [pk]", self.gold)
        self.assertEqual(">", dbml_refs(self.gold)["fact_balance_snapshot_account_id"][1])
        self.assertEqual(">", dbml_refs(self.gold)["feat_user_behavior_user_id"][1])
        self.assertEqual(">", dbml_refs(self.silver)["balance_snapshots_account_id"][1])

    def test_physical_nullability_is_stated_and_not_changed(self):
        for zone in ("silver", "gold", "features"):
            for table in self.observed["zones"][zone]["tables"].values():
                self.assertTrue(all(column["nullable"] for column in table["columns"]))
        self.assertIn("every Delta column is physically nullable", self.gold)
        self.assertIn("not physical constraints", self.silver)

    def test_builder_only_edges_are_never_called_validated(self):
        refs = [line for line in self.gold.splitlines() if line.startswith("// obt_transaction_enriched")]
        self.assertEqual(1, len(refs))
        self.assertIn("builder_only", refs[0])
        self.assertNotIn("validated by", refs[0])
        fraud = [line for line in self.silver.splitlines() if line.startswith("// fraud_labels")]
        self.assertIn("builder_only", fraud[0])
        self.assertIn("rule fraud_labels.transaction_fk exists but is not counted as validation", fraud[0])

    def test_dbml_is_deterministic(self):
        self.assertEqual(self.gold, render_dbml(("gold", "features"), self.observed, self.document))


class EdgeSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.observed, self.document = inputs()

    def semantics(self, table, column):
        item = next(item for item in load_relationships(self.document)
                    if item.table == table and item.column == column)
        return edge_semantics(item, self.document)

    def test_cardinality_comes_from_keys(self):
        self.assertEqual("many-to-one", self.semantics("fact_transactions", "user_id").cardinality)
        self.assertEqual("one-to-one", self.semantics("feat_user_90d", "user_id").cardinality)
        self.assertEqual("one-to-one", self.semantics("fraud_labels", "transaction_id").cardinality)
        self.assertTrue(self.semantics("fact_transactions", "merchant_id").parent_optional)
        self.assertFalse(self.semantics("opt_merchant_performance", "merchant_id").child_optional)

    def test_fraud_labels_grain_is_one_row_per_transaction(self):
        self.assertEqual(["transaction_id"], self.document["keys"]["silver"]["fraud_labels"])
        source = (ROOT / "data_platform/quality/validate_silver.py").read_text(encoding="utf-8")
        self.assertIn('validate_unique_key(df, "fraud_labels", ["transaction_id"])', source)

    def test_complete_coverage_matches_the_builders(self):
        complete = {(item.table, item.target_table) for item in load_relationships(self.document)
                    if item.coverage == "complete"}
        self.assertEqual({("fraud_labels", "transactions"), ("obt_transaction_enriched", "fact_transactions"),
                          ("feat_user_90d", "dim_user"), ("opt_merchant_performance", "dim_merchant")}, complete)
        analytics = (ROOT / "data_platform/processing/spark/gold/analytics_builder.py").read_text(encoding="utf-8")
        self.assertRegex(analytics, r"dim_merchant_df\s*\.join\(\s*merchant_metrics,\s*on=\"merchant_id\",\s*how=\"left\"")
        obt = (ROOT / "data_platform/processing/spark/gold/obt_builder.py").read_text(encoding="utf-8")
        self.assertRegex(obt, r"return \(\s*fact_transactions_df\s*\.join\(")
        # Every join of the fact rows onto a uniquely keyed dimension is a left join,
        # so the OBT keeps exactly one row per fact row.
        chain = obt.split("return (", 1)[1].split(".select(", 1)[0]
        self.assertEqual(5, chain.count(".join("))
        self.assertEqual(5, chain.count('how="left"'))

    def test_invalid_contracts_are_rejected(self):
        cases = []
        document = copy.deepcopy(self.document)
        document["relationships"]["gold"][0]["coverage"] = "complete"  # many-to-one
        cases.append(document)
        document = copy.deepcopy(self.document)
        for item in document["relationships"]["gold"]:
            if item["table"] == "feat_user_90d":
                item["nullable"] = True  # a key column
        cases.append(document)
        document = copy.deepcopy(self.document)
        document["relationships"]["gold"][0]["references"] = "dim_user.full_name"  # not the parent key
        cases.append(document)
        for document in cases:
            with self.subTest():
                self.assertTrue(validate_relationships(self.observed, document))
                with self.assertRaises(ValueError):
                    render_all(self.observed, document)


class CheckEvidenceTest(unittest.TestCase):
    def test_cloud_run_checks_only_the_five_fact_transaction_references(self):
        source = (ROOT / "ops/gcp/summarize_spark_output.py").read_text(encoding="utf-8")
        self.assertEqual({("fact_transactions", key, dimension, key) for dimension, key in (
            ("dim_user", "user_id"), ("dim_account", "account_id"), ("dim_merchant", "merchant_id"),
            ("dim_device", "device_id"), ("dim_date", "date_key"))}, set(cloud_reference_checks(source)))

    def test_every_validated_edge_has_a_validator_rule_and_builder_only_edges_are_known(self):
        evidence = load_evidence(ROOT)
        _, document = inputs()
        for item in load_relationships(document):
            key = (item.table, item.column, item.target_table, item.target_column)
            with self.subTest(key):
                if item.enforcement == "validated":
                    self.assertIn(key, evidence.validators)
        builder_only = {(item.table, item.target_table) for item in load_relationships(document)
                        if item.enforcement == "builder_only"}
        self.assertEqual({("fraud_labels", "transactions"), ("obt_transaction_enriched", "fact_transactions")},
                         builder_only)

    def test_validators_check_no_edge_outside_the_contract(self):
        _, document = inputs()
        contract = {(item.table, item.column, item.target_table, item.target_column)
                    for item in load_relationships(document)}
        self.assertLessEqual(set(load_evidence(ROOT).validators), contract)

    def test_the_fraud_label_check_runs_only_behind_its_flag(self):
        source = (ROOT / "data_platform/quality/validate_silver.py").read_text(encoding="utf-8")
        self.assertIn(("fraud_labels", "transaction_id", "transactions", "transaction_id"), validator_edges(source))
        self.assertIn("if include_fraud_labels:\n            all_results.extend(\n"
                      "                validate_exact_transaction_coverage(", source)

    def test_empty_evidence_marks_nothing_validated_by_rule(self):
        observed, document = inputs()
        text = render_dbml(("silver",), observed, document, Evidence())
        self.assertNotIn("validated by rule", text)
        self.assertNotIn("; checked on the 100GB", text)


class OfficialParserTest(unittest.TestCase):
    """Parse the committed DBML with the official @dbml/core parser, when available.

    Set DBML_CORE_DIR to a directory holding node_modules/@dbml/core (pinned 10.3.1
    in the validation run); the test is skipped otherwise, never faked.
    """

    def test_committed_dbml_parses_with_expected_cardinalities(self):
        import os
        import shutil
        import subprocess

        core = os.environ.get("DBML_CORE_DIR")
        if not core or not shutil.which("node"):
            self.skipTest("DBML_CORE_DIR or node not available")
        script = (
            "const fs=require('fs');const core=require(process.argv[1]+'/node_modules/@dbml/core');"
            "const out={};for(const f of process.argv.slice(2)){const db=core.Parser.parse(fs.readFileSync(f,'utf8'),'dbmlv2');"
            "out[f]=db.schemas.flatMap(s=>s.refs).map(r=>r.endpoints.map(e=>e.tableName+'.'+e.fieldNames.join(',')+'='+e.relation).join(' '));}"
            "console.log(JSON.stringify(out));"
        )
        files = [str(GENERATED / "silver.dbml"), str(GENERATED / "gold.dbml")]
        result = subprocess.run(["node", "-e", script, core, *files], capture_output=True, text=True, timeout=60,
                                check=True)
        refs = json.loads(result.stdout)
        silver, gold = refs[files[0]], refs[files[1]]
        self.assertEqual(11, len(silver))
        self.assertEqual(20, len(gold))
        self.assertIn("transactions.merchant_id=* merchants.merchant_id=0..1", silver)
        self.assertIn("transactions.transaction_id=1 fraud_labels.transaction_id=1", silver)
        self.assertIn("dim_user.user_id=1 feat_user_90d.user_id=1", gold)
        self.assertIn("fact_transactions.counterparty_account_id=* dim_account.account_id=0..1", gold)
        self.assertIn("fact_transactions.user_id=* dim_user.user_id=1", gold)
