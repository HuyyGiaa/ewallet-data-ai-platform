"""Schema Design: campaign-bound export, relationship contract and dbdiagram.io DBML.

The renderer is exercised on a test-only snapshot built from the real 5GB column
names in the campaign's ``comparison.json`` (all 34 Delta tables). Its column
*types* are derived from the column names in this test, so the snapshot proves
the rendering logic, never the physical schema; the committed generated files
are produced only from the owner's metadata export.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
import json
import re
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from data_platform.contracts.schema_inventory import zone_inventory
from data_platform.schema import export_observed_schemas as exporter
from data_platform.schema.render_schema_diagrams import (
    CANONICAL_BENCHMARK,
    CANONICAL_DATASET,
    CANONICAL_EVIDENCE,
    GENERATED_DIR,
    RELATIONSHIPS,
    SchemaRenderError,
    edge_semantics,
    gold_role,
    load_relationships,
    load_rule_evidence,
    render_all,
    render_trino_metadata_queries,
    render_trino_provenance_queries,
    render_trino_registration,
    require_current_snapshot,
    validate_contract,
)


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / CANONICAL_EVIDENCE
GENERATED = ROOT / GENERATED_DIR
EVIDENCE_QUERIES = ROOT / "data_platform/schema/sql/schema_evidence_queries.sql"
VIZ = ROOT / "infra/docker/schema-viz"
COMPARISON = json.loads((EVIDENCE / "comparison.json").read_text(encoding="utf-8"))
CAMPAIGNS, RULES = load_rule_evidence(EVIDENCE, CANONICAL_BENCHMARK, CANONICAL_DATASET)
DOCUMENT = yaml.safe_load((ROOT / RELATIONSHIPS).read_text(encoding="utf-8"))
PARTITIONS = {("silver", "transactions"): ["event_date"], ("gold", "fact_transactions"): ["event_date"],
              ("gold", "fact_login_events"): ["event_date"], ("gold", "obt_transaction_enriched"): ["event_date"],
              ("gold", "fact_balance_snapshot"): ["snapshot_date"]}


def _fixture_type(name: str) -> str:
    """Test-only type for a column name; consistent across a relationship by construction."""

    if name in ("timestamp", "event_timestamp", "created", "created_timestamp", "ingested_at") or name.endswith("_ts"):
        return "timestamp"
    if name in ("event_date", "snapshot_date", "date") or name.endswith("_date"):
        return "date"
    if name == "date_key":
        return "integer"
    if name.startswith("is_"):
        return "boolean"
    if (name.endswith(("_id", "_sk", "_name", "_type", "_source")) or
            name in ("type", "status", "currency", "channel", "category", "label", "fraud_type", "kyc_level")):
        return "string"
    return "double"


def snapshot(profile: str = "optimized") -> dict:
    """A campaign-bound snapshot: real 5GB table and column names, test-only types."""

    campaign = CAMPAIGNS[profile]
    zone_of = exporter.COMPARISON_ZONES
    zones = {stage.zone: {"run_id": stage.run_id, "root": stage.output_root, "tables": {}}
             for stage in campaign.stages.values()}
    for stage in COMPARISON["stages"]:
        zone = zone_of[stage["stage"]]
        for table in stage["tables"]:
            names = [*table["compared_columns"], *table["excluded_audit_columns"]]
            zones[zone]["tables"][table["table"]] = {
                "columns": [{"name": name, "type": _fixture_type(name), "nullable": True} for name in names],
                "partition_columns": PARTITIONS.get((zone, table["table"]), []),
                "delta_version": 0, "commit_count": 1,
            }
    inventory = zone_inventory(campaign.dataset_version)
    source = {"dataset_version": campaign.dataset_version, "plan_digest": campaign.source_plan_digest,
              "manifest_sha256": campaign.source_manifest_sha256, "root": campaign.source_root,
              "tables": {name: {"logical_table": "transactions" if name.startswith("transactions_v") else name,
                                "rows": 1, "files": 1, "bytes": 1, "schema_sha256": "0" * 64,
                                "columns": [{"name": "id", "type": "string", "nullable": True}]}
                         for name in inventory.source_physical}}
    return {"schema_version": exporter.OBSERVED_SCHEMA_VERSION,
            "provenance": exporter.provenance(campaign, "2026-11-11T00:00:00+00:00"),
            "source": source, "zones": zones}


def render(observed: dict | None = None, document: dict | None = None) -> dict[str, str]:
    return render_all(observed or snapshot(), document or DOCUMENT, CAMPAIGNS, RULES, COMPARISON)


def statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [item.strip() for item in body.split(";") if item.strip()]


def dbml_structure(text: str) -> dict:
    """Strict structural check of the DBML subset the renderer emits.

    Raises AssertionError on unbalanced blocks, duplicate tables, a Ref or group to
    an undeclared table or column, or an unknown Ref operator.
    """

    tables: dict[str, set[str]] = {}
    refs, groups, depth, current = [], {}, 0, None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        quoted = re.sub(r"'(?:\\.|[^'\\])*'", "''", stripped)
        if depth == 0:
            if match := re.match(r"^Table ([a-z_]+\.[a-z0-9_]+) (\[[^\]]*\] )?\{$", quoted):
                assert match.group(1) not in tables, f"duplicate table at line {number}"
                current = ("table", match.group(1))
                tables[match.group(1)] = set()
            elif match := re.match(r"^TableGroup ([a-z_]+) \{$", quoted):
                current = ("group", match.group(1))
                groups[match.group(1)] = []
            elif re.match(r"^Project [a-z0-9_]+ \{$", quoted):
                current = ("project", None)
            elif match := re.match(r"^Ref (fk_[a-z0-9_]+): ([a-z_]+\.[a-z0-9_]+)\.([a-z0-9_]+) ([<>-]) "
                                   r"([a-z_]+\.[a-z0-9_]+)\.([a-z0-9_]+)$", quoted):
                refs.append(match.groups())
                continue
            else:
                raise AssertionError(f"unexpected top-level DBML at line {number}: {stripped}")
            depth = 1
            continue
        if quoted == "}":
            depth -= 1
            if depth == 0:
                current = None
            continue
        if quoted in ("indexes {",):
            depth += 1
            continue
        if current and current[0] == "table" and depth == 1:
            match = re.match(r"^([a-z0-9_]+) [a-z_]+( \[[^\]]+\])?$", quoted)
            assert match, f"bad column at line {number}: {stripped}"
            tables[current[1]].add(match.group(1))
        elif current and current[0] == "table" and depth == 2:
            assert re.match(r"^\([a-z0-9_, ]+\) \[(pk|unique, name: '')\]$", quoted), f"bad index at {number}"
        elif current and current[0] == "group":
            groups[current[1]].append(quoted)
        elif current and current[0] == "project":
            assert re.match(r"^(database_type|Note): ''$", quoted), f"bad project setting at {number}"
    assert depth == 0, "unbalanced braces"
    for name, child, child_column, operator, parent, parent_column in refs:
        assert child in tables and child_column in tables[child], f"{name}: unknown child {child}.{child_column}"
        assert parent in tables and parent_column in tables[parent], f"{name}: unknown parent {parent}.{parent_column}"
    for name, members in groups.items():
        assert members and set(members) <= set(tables), f"group {name} names an undeclared table"
    return {"tables": tables, "refs": refs, "groups": groups}


class InventoryTest(unittest.TestCase):
    def test_the_5gb_inventory_is_10_source_9_9_12_4(self):
        counts = zone_inventory(CANONICAL_DATASET).counts()
        self.assertEqual({"source_physical": 10, "source_logical": 9, "bronze": 9, "silver": 9, "gold": 12,
                          "features": 4}, counts)

    def test_the_comparison_saw_exactly_the_expected_delta_tables(self):
        inventory = zone_inventory(CANONICAL_DATASET)
        for stage in COMPARISON["stages"]:
            zone = exporter.COMPARISON_ZONES[stage["stage"]]
            with self.subTest(zone):
                inventory.require_tables(zone, [table["table"] for table in stage["tables"]])


class RelationshipContractTest(unittest.TestCase):
    """Keys and relationships against the real 5GB column names and rule results."""

    def test_the_contract_matches_the_real_5gb_columns_and_rules(self):
        self.assertEqual([], validate_contract(snapshot(), DOCUMENT, RULES))

    def test_every_logical_key_equals_the_comparison_key(self):
        compared = {(exporter.COMPARISON_ZONES[stage["stage"]], table["table"]): table["keys"]
                    for stage in COMPARISON["stages"] for table in stage["tables"] if table["keys"]}
        alternate = {(zone, table): [item["columns"] for item in items]
                     for zone, tables in DOCUMENT["unique"].items() for table, items in tables.items()}
        for (zone, table), keys in compared.items():
            with self.subTest(f"{zone}.{table}"):
                declared = DOCUMENT["keys"][zone][table]
                self.assertTrue(keys == declared or keys in alternate.get((zone, table), []))

    def test_every_validated_edge_names_a_rule_that_passed_in_both_profiles(self):
        validated = [item for item in load_relationships(DOCUMENT) if item.enforcement == "validated"]
        self.assertEqual(32, len(validated))  # 12 Silver, 16 Gold, 4 features
        for item in validated:
            with self.subTest(item.rule):
                self.assertTrue(RULES.passed_everywhere(item.rule))

    def test_scd2_is_many_versions_to_one_merchant(self):
        edge = next(item for item in load_relationships(DOCUMENT) if item.table == "dim_merchant_scd2")
        self.assertEqual(("dim_merchant", "merchant_id"), (edge.target_table, edge.target_column))
        self.assertEqual("many-to-one", edge_semantics(edge, DOCUMENT).cardinality)
        self.assertEqual([["merchant_id", "valid_from_ts"]],
                         [item["columns"] for item in DOCUMENT["unique"]["gold"]["dim_merchant_scd2"]])

    def test_only_merchant_and_counterparty_references_are_optional(self):
        optional = {(item.table, item.column) for item in load_relationships(DOCUMENT) if item.nullable}
        self.assertEqual({("transactions", "merchant_id"), ("transactions", "counterparty_account_id"),
                          ("fact_transactions", "merchant_id"), ("fact_transactions", "counterparty_account_id")},
                         optional)

    def test_a_wrong_reference_is_rejected(self):
        document = copy.deepcopy(DOCUMENT)
        document["relationships"]["gold"][3]["references"] = "dim_user.user_name"
        self.assertTrue(any("not observed" in item for item in validate_contract(snapshot(), document, RULES)))

    def test_an_edge_to_a_non_key_column_is_rejected(self):
        document = copy.deepcopy(DOCUMENT)
        document["keys"]["gold"]["dim_user"] = ["user_id", "created_at"]
        self.assertTrue(any("whole key" in item for item in validate_contract(snapshot(), document, RULES)))

    def test_an_invalid_logical_key_is_rejected(self):
        document = copy.deepcopy(DOCUMENT)
        document["keys"]["gold"]["dim_merchant_scd2"] = ["version_id"]
        self.assertTrue(any("dim_merchant_scd2" in item for item in validate_contract(snapshot(), document, RULES)))

    def test_a_validated_edge_with_an_unexecuted_rule_is_rejected(self):
        document = copy.deepcopy(DOCUMENT)
        document["relationships"]["silver"][0]["rule"] = "accounts.user_id_exists_in_nowhere"
        self.assertTrue(any("did not pass" in item for item in validate_contract(snapshot(), document, RULES)))

    def test_a_type_mismatch_across_a_relationship_is_rejected(self):
        observed = snapshot()
        for column in observed["zones"]["gold"]["tables"]["fact_transactions"]["columns"]:
            if column["name"] == "user_id":
                column["type"] = "long"
        self.assertTrue(any("differs from its parent type" in item
                            for item in validate_contract(observed, DOCUMENT, RULES)))


class SnapshotIdentityTest(unittest.TestCase):
    def test_a_legacy_100gb_snapshot_is_refused(self):
        legacy = {"schema_version": "ewallet-observed-schemas-v1", "zones": {}, "source": {}}
        with self.assertRaisesRegex(SchemaRenderError, "legacy 100GB"):
            require_current_snapshot(legacy, CAMPAIGNS)

    def test_a_snapshot_of_another_dataset_is_refused(self):
        observed = snapshot()
        observed["provenance"]["dataset_version"] = "canonical-100gb-v1"
        with self.assertRaisesRegex(SchemaRenderError, "dataset_version"):
            require_current_snapshot(observed, CAMPAIGNS)

    def test_a_mismatched_source_manifest_is_refused(self):
        observed = snapshot()
        observed["provenance"]["source_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(SchemaRenderError, "source_manifest_sha256"):
            require_current_snapshot(observed, CAMPAIGNS)

    def test_a_missing_table_is_refused(self):
        observed = snapshot()
        del observed["zones"]["gold"]["tables"]["dim_merchant_scd2"]
        with self.assertRaisesRegex(SchemaRenderError, "missing=\\['dim_merchant_scd2'\\]"):
            require_current_snapshot(observed, CAMPAIGNS)

    def test_missing_scd2_columns_are_refused(self):
        observed = snapshot()
        table = observed["zones"]["gold"]["tables"]["dim_merchant_scd2"]
        table["columns"] = [item for item in table["columns"] if item["name"] not in ("valid_to_ts", "is_current")]
        with self.assertRaisesRegex(SchemaRenderError, "dim_merchant_scd2"):
            render(observed)

    def test_both_profiles_render(self):
        for profile in ("baseline", "optimized"):
            with self.subTest(profile):
                self.assertIn("gold.dbml", render(snapshot(profile)))


class DbmlTest(unittest.TestCase):
    def setUp(self):
        self.files = render()

    def test_silver_dbml_has_9_tables_and_12_refs(self):
        structure = dbml_structure(self.files["silver.dbml"])
        self.assertEqual(9, len(structure["tables"]))
        self.assertIn("silver_zone.merchant_history", structure["tables"])
        self.assertEqual(12, len(structure["refs"]))
        self.assertEqual(3, len(structure["groups"]))

    def test_gold_dbml_has_12_core_and_4_feature_tables(self):
        structure = dbml_structure(self.files["gold.dbml"])
        names = set(structure["tables"])
        self.assertEqual(16, len(names))
        self.assertEqual(4, len({name for name in names if re.match(r"gold_zone\.feat_\w+_behavior$", name)}))
        self.assertIn("gold_zone.dim_merchant_scd2", names)
        self.assertEqual(21, len(structure["refs"]))

    def test_refs_point_from_fact_to_dimension_on_the_parent_key(self):
        refs = dbml_structure(self.files["gold.dbml"])["refs"]
        fact_refs = [ref for ref in refs if ref[1].startswith("gold_zone.fact_")]
        self.assertEqual(11, len(fact_refs))
        for name, child, column, operator, parent, parent_column in fact_refs:
            self.assertEqual(">", operator)
            self.assertTrue(parent.startswith("gold_zone.dim_"))
            self.assertTrue(column.endswith(parent_column), column)

    def test_scd2_and_feature_timestamps_are_annotated(self):
        gold = self.files["gold.dbml"]
        self.assertIn("Ref fk_dim_merchant_scd2_merchant_id: gold_zone.dim_merchant_scd2.merchant_id > "
                      "gold_zone.dim_merchant.merchant_id", gold)
        self.assertIn("(merchant_id, valid_from_ts) [unique", gold)
        self.assertIn("dim_merchant_scd2.transactions_resolve_to_one_version", gold)
        self.assertRegex(gold, r"  event_timestamp timestamp \[note: 'Event time")
        self.assertRegex(gold, r"  created timestamp \[note: 'Materialization time")

    def test_only_valid_dbml_ref_operators_are_used(self):
        for name in ("silver.dbml", "gold.dbml"):
            for line in self.files[name].splitlines():
                if line.startswith("Ref "):
                    self.assertNotRegex(line, r"[<>-]\?")

    def test_output_is_deterministic_and_carries_provenance(self):
        self.assertEqual(self.files, render())
        self.assertIn(CANONICAL_BENCHMARK, self.files["gold.dbml"])
        self.assertIn("profile optimized", self.files["silver.dbml"])

    def test_committed_generated_files_match_the_committed_snapshot(self):
        observed_path = GENERATED / "observed_schemas.json"
        if not observed_path.is_file():
            self.skipTest("OWNER_METADATA_EXPORT_PENDING: no observed_schemas.json committed yet")
        observed = json.loads(observed_path.read_text(encoding="utf-8"))
        for name, content in render(observed).items():
            with self.subTest(name):
                self.assertEqual(content, (GENERATED / name).read_text(encoding="utf-8"))


class NamingConventionTest(unittest.TestCase):
    def test_bronze_and_silver_keep_business_names_in_zone_schemas(self):
        inventory = zone_inventory(CANONICAL_DATASET)
        for table in (*inventory.bronze, *inventory.silver):
            self.assertRegex(table, r"^[a-z]+(_[a-z]+)*$")
            self.assertFalse(table.startswith(("raw_", "stg_", "dim_", "fact_")))

    def test_every_gold_and_feature_table_carries_a_role_prefix(self):
        inventory = zone_inventory(CANONICAL_DATASET)
        for table in (*inventory.gold, *inventory.features):
            self.assertNotEqual("Other", gold_role(table), table)


class TrinoSqlTest(unittest.TestCase):
    def setUp(self):
        self.observed = snapshot()

    def test_registration_only_writes_the_local_metastore(self):
        items = statements(render_trino_registration(self.observed))
        for item in items:
            self.assertRegex(item, r"^(CREATE SCHEMA IF NOT EXISTS|CALL delta\.system\.register_table\("
                                   r"|CREATE TABLE IF NOT EXISTS hive\.source_zone\.)")
        self.assertEqual(34, sum(item.startswith("CALL") for item in items))
        self.assertEqual(10, sum(item.startswith("CREATE TABLE") for item in items))

    def test_every_registered_location_is_a_campaign_table(self):
        sql = render_trino_registration(self.observed)
        found = set(re.findall(r"location => '([^']+)'", sql)) | set(re.findall(r"external_location = '([^']+)'", sql))
        self.assertTrue(all("ewallet-data-ai-platform-hg26-rehearsal-5gb-data" in item for item in found))
        self.assertTrue(all("ab-20261110225456-optimized" in item for item in found if "/lakehouse/" in item))

    def test_metadata_queries_are_read_only_and_never_scan_data(self):
        for item in statements(render_trino_metadata_queries(self.observed)):
            self.assertRegex(item, r"^(SHOW TABLES|SELECT|DESCRIBE)(\s|$)")
            self.assertNotRegex(item.upper(), r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|CALL|OPTIMIZE|VACUUM)\b")
            if item.startswith("SELECT"):
                self.assertRegex(item, r"FROM \w+\.(information_schema\.\w+|\w+\.\"\w+\$history\")")

    def test_provenance_queries_only_show_table_definitions(self):
        for item in statements(render_trino_provenance_queries(self.observed)):
            self.assertRegex(item, r"^SHOW CREATE TABLE (delta|hive)\.\w+_zone\.\w+$")

    def test_the_evidence_queries_are_read_only(self):
        sql = EVIDENCE_QUERIES.read_text(encoding="utf-8")
        items = statements(sql)
        self.assertGreaterEqual(len(items), 12)
        for item in items:
            self.assertRegex(item, r"^SELECT\s")
            self.assertNotRegex(item.upper(), r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|CALL|OPTIMIZE|VACUUM)\b")

    def test_generated_artifacts_live_with_the_implementation(self):
        """Schema artifacts belong to data_platform/schema; docs/edai1/schema keeps only reports."""

        self.assertEqual(GENERATED_DIR, Path("data_platform/schema/generated"))
        reports = ROOT / "docs/edai1/schema"
        self.assertEqual(sorted(path.suffix for path in reports.rglob("*") if path.is_file()),
                         [".md"] * sum(1 for path in reports.rglob("*") if path.is_file()))
        for name in ("silver.dbml", "gold.dbml", "trino_register_tables.sql"):
            self.assertIn(f"Generated by {GENERATED_DIR.parent}/render_schema_diagrams.py",
                          (GENERATED / name).read_text(encoding="utf-8").splitlines()[0])


class ExporterTest(unittest.TestCase):
    """The metadata-only exporter against an in-memory GCS."""

    campaign = CAMPAIGNS["optimized"]

    @staticmethod
    def parquet() -> tuple[bytes, str]:
        buffer = io.BytesIO()
        pq.write_table(pa.table({"id": pa.array(["u1"])}), buffer)
        data = buffer.getvalue()
        return data, hashlib.sha256(str(pq.read_schema(pa.BufferReader(data))).encode()).hexdigest()

    def fake_gcs(self, *, manifest_tables=None, drop_table=None, commits=("00000000000000000000.json",)):
        observed = snapshot()
        data, digest = self.parquet()
        inventory = zone_inventory(self.campaign.dataset_version)
        tables = manifest_tables or list(inventory.source_physical)
        manifest = json.dumps({"dataset_version": self.campaign.dataset_version, "status": "COMPLETE",
                               "plan_digest": "p", "tables": {name: {
                                   "rows": 1, "bytes": 1, "schema_sha256": [digest],
                                   "files": [{"path": f"{name}/shard-00000.parquet"}]} for name in tables}}).encode()
        objects: dict[str, bytes] = {self.campaign.source_manifest_uri: manifest}
        prefixes: dict[str, list[str]] = {}
        for zone, entry in observed["zones"].items():
            names = [name for name in entry["tables"] if name != drop_table]
            prefixes[f"{entry['root']}/"] = [f"{entry['root']}/{name}/" for name in names]
            listed = []
            for name in names:
                schema = {"type": "struct", "fields": [
                    {"name": column["name"], "type": column["type"], "nullable": True, "metadata": {}}
                    for column in entry["tables"][name]["columns"]]}
                for index, commit in enumerate(commits):
                    uri = f"{entry['root']}/{name}/_delta_log/{commit}"
                    action = {"metaData": {"schemaString": json.dumps(schema), "partitionColumns": []}}
                    objects[uri] = (json.dumps(action) if index == 0 else json.dumps({"commitInfo": {}})).encode()
                    listed.append(uri)
            prefixes[f"{entry['root']}/*/_delta_log/*.json"] = listed
        return objects, prefixes, data

    def export(self, objects, prefixes, data, campaign=None):
        return exporter.export_campaign(campaign or self.campaign, read=lambda uri: objects[uri],
                                        list_objects=lambda pattern: prefixes[pattern],
                                        read_tail=lambda uri, size: data[-size:], exported_at="t")

    def test_export_is_bound_to_the_campaign_and_matches_the_comparison(self):
        objects, prefixes, data = self.fake_gcs()
        manifest_sha = hashlib.sha256(objects[self.campaign.source_manifest_uri]).hexdigest()
        # The in-memory manifest is not the real one, so pin its checksum on a copy of the campaign.
        document = self.export(objects, prefixes, data,
                               dataclasses.replace(self.campaign, source_manifest_sha256=manifest_sha))
        self.assertEqual("optimized", document["provenance"]["spark_profile"])
        self.assertEqual(10, len(document["source"]["tables"]))
        self.assertEqual({"bronze": 9, "silver": 9, "gold": 12, "features": 4},
                         {zone: len(value["tables"]) for zone, value in document["zones"].items()})
        document["provenance"]["source_manifest_sha256"] = COMPARISON["source_manifest_sha256"]
        self.assertEqual([], exporter.comparison_problems(document, COMPARISON))

    def test_a_source_manifest_with_another_checksum_is_refused(self):
        with self.assertRaisesRegex(exporter.ExportError, "source manifest SHA-256"):
            self.export(*self.fake_gcs())

    def test_a_missing_delta_table_is_refused(self):
        objects, prefixes, data = self.fake_gcs(drop_table="dim_merchant_scd2")
        with self.assertRaisesRegex(exporter.ExportError, "dim_merchant_scd2"):
            self.export(objects, prefixes, data)

    def test_a_gap_in_the_delta_log_is_refused(self):
        with self.assertRaisesRegex(exporter.ExportError, "contiguous"):
            exporter.delta_table_schema("gs://b/t", ["gs://b/t/_delta_log/00000000000000000001.json"],
                                        lambda uri: b"")

    def test_the_last_metadata_action_wins(self):
        first = json.dumps({"metaData": {"schemaString": json.dumps({"fields": [
            {"name": "a", "type": "string", "nullable": True}]}), "partitionColumns": []}})
        second = json.dumps({"metaData": {"schemaString": json.dumps({"fields": [
            {"name": "a", "type": "string", "nullable": True}, {"name": "b", "type": "long", "nullable": True}]}),
            "partitionColumns": ["a"]}})
        objects = {"gs://b/t/_delta_log/00000000000000000000.json": first.encode(),
                   "gs://b/t/_delta_log/00000000000000000001.json": second.encode()}
        result = exporter.delta_table_schema("gs://b/t", list(objects), objects.__getitem__)
        self.assertEqual(["a", "b"], [item["name"] for item in result["columns"]])
        self.assertEqual((1, 2, ["a"]), (result["delta_version"], result["commit_count"], result["partition_columns"]))

    def test_comparison_cross_check_detects_another_run(self):
        observed = snapshot()
        observed["zones"]["silver"]["tables"]["transactions"]["columns"].pop()
        self.assertTrue(any("silver.transactions" in item for item in exporter.comparison_problems(observed, COMPARISON)))

    def test_the_footer_gives_columns_and_the_generator_schema_hash(self):
        data, digest = self.parquet()
        columns, found = exporter.parse_parquet_footer(data[-4096:])
        self.assertEqual((["id"], digest), ([item["name"] for item in columns], found))
        with self.assertRaises(ValueError):
            exporter.parse_parquet_footer(b"not parquet")


class ReadOnlyStackTest(unittest.TestCase):
    def test_only_the_registrar_may_write_and_only_register_table_is_callable(self):
        rules = json.loads((VIZ / "rules.json").read_text(encoding="utf-8"))
        writers = [rule for rule in rules["catalogs"] if rule["allow"] != "read-only"]
        self.assertEqual([{"user": "registrar", "catalog": "(delta|hive)", "allow": "all"}], writers)
        self.assertEqual([("registrar", "delta", "system", "register_table")],
                         [(rule["user"], rule["catalog"], rule["schema"], rule["procedure"])
                          for rule in rules["procedures"]])

    def test_the_stack_is_isolated_and_reads_gcs_with_mounted_adc(self):
        compose = yaml.safe_load((VIZ / "compose.yml").read_text(encoding="utf-8"))
        service = compose["services"]["trino"]
        self.assertEqual(["127.0.0.1:8091:8080"], service["ports"])
        self.assertTrue(any(volume.endswith("/var/run/gcp/adc.json:ro") for volume in service["volumes"]))
        for name in ("delta.properties", "hive.properties"):
            text = (VIZ / "catalog" / name).read_text(encoding="utf-8")
            self.assertIn("hive.metastore.catalog.dir=local:///metastore/", text)
            self.assertNotRegex(text, r"(?i)minio|s3\.|aws|password")


if __name__ == "__main__":
    unittest.main()
