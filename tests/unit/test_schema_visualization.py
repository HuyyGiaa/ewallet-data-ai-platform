"""The DBeaver/Trino schema visualization stays tied to real schemas and stays read-only."""

from __future__ import annotations

import copy
import io
import json
import re
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from data_platform.storage.scripts.config import (
    BRONZE_SCHEMA,
    CORE_GOLD_TABLES,
    GOLD_SCHEMA,
    GOLD_TABLES,
    SILVER_SCHEMA,
    TRINO_CATALOG,
    offline_tables,
)
from ops.schema.export_observed_schemas import export_source, parse_parquet_footer
from ops.schema.render_schema_diagrams import (
    ERD_DIMENSIONS,
    ERD_FACTS,
    RELATIONSHIPS,
    TRINO_SCHEMAS,
    gold_erd_edges,
    gold_role,
    render_trino_metadata_queries,
    render_trino_provenance_queries,
    render_trino_registration,
)


ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "docs/edai1/schema/generated"
VIZ = ROOT / "infra/docker/schema-viz"
DOCS = (
    ROOT / "docs/edai1/schema/SCHEMA_DESIGN.md",
    ROOT / "docs/edai1/schema/ALL_ZONES_SCHEMA.md",
    ROOT / "docs/runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md",
    GENERATED / "GOLD_ERD.md",
)


def inputs():
    observed = json.loads((GENERATED / "observed_schemas.json").read_text(encoding="utf-8"))
    return observed, yaml.safe_load((ROOT / RELATIONSHIPS).read_text(encoding="utf-8"))


def statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [item.strip() for item in body.split(";") if item.strip()]


def columns(observed, zone, table):
    return {item["name"]: item["type"] for item in observed["zones"][zone]["tables"][table]["columns"]}


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.observed, _ = inputs()

    def test_logical_inventory_is_8_8_8_15(self):
        source = {item["logical_table"] for item in self.observed["source"]["tables"].values()}
        zones = self.observed["zones"]
        counts = {
            "source": len(source),
            "bronze": len(zones["bronze"]["tables"]),
            "silver": len(zones["silver"]["tables"]),
            "gold": len(zones["gold"]["tables"]) + len(zones["features"]["tables"]),
        }
        self.assertEqual({"source": 8, "bronze": 8, "silver": 8, "gold": 15}, counts)
        self.assertEqual(39, sum(counts.values()))

    def test_observed_tables_equal_the_configured_inventory(self):
        zones = self.observed["zones"]
        logical_source = {item["logical_table"] for item in self.observed["source"]["tables"].values()}
        self.assertEqual(set(offline_tables(include_fraud_labels=True)), logical_source)
        self.assertEqual(logical_source, set(zones["bronze"]["tables"]))
        self.assertEqual(logical_source, set(zones["silver"]["tables"]))
        self.assertEqual(set(GOLD_TABLES), set(zones["gold"]["tables"]) | set(zones["features"]["tables"]))

    def test_source_has_nine_physical_datasets_for_eight_logical_tables(self):
        physical = self.observed["source"]["tables"]
        self.assertEqual(9, len(physical))
        self.assertEqual({"transactions_v1", "transactions_v2"},
                         {name for name, item in physical.items() if item["logical_table"] == "transactions"})

    def test_bronze_transactions_is_the_union_of_both_source_versions(self):
        source = self.observed["source"]["tables"]
        v1 = {item["name"] for item in source["transactions_v1"]["columns"]}
        v2 = {item["name"] for item in source["transactions_v2"]["columns"]}
        self.assertEqual({"channel"}, v2 - v1)
        self.assertEqual(v1 | v2, set(columns(self.observed, "bronze", "transactions")))

    def test_source_inventory_holds_metadata_only(self):
        for table in self.observed["source"]["tables"].values():
            self.assertEqual({"logical_table", "rows", "files", "bytes", "schema_sha256", "columns"}, set(table))
            for column in table["columns"]:
                self.assertEqual({"name", "type", "nullable"}, set(column))


class GoldErdTest(unittest.TestCase):
    def setUp(self):
        self.observed, self.document = inputs()
        self.edges = gold_erd_edges(self.document)

    def test_the_erd_covers_five_dimensions_and_three_facts(self):
        self.assertEqual(set(ERD_DIMENSIONS + ERD_FACTS), {edge.child for edge in self.edges}
                         | {edge.parent for edge in self.edges})
        self.assertTrue(set(ERD_DIMENSIONS + ERD_FACTS) <= set(CORE_GOLD_TABLES))
        self.assertEqual(13, len(self.edges))

    def test_every_edge_joins_real_columns_of_the_same_type(self):
        for edge in self.edges:
            with self.subTest(f"{edge.child}.{edge.column}"):
                child = columns(self.observed, "gold", edge.child)
                parent = columns(self.observed, "gold", edge.parent)
                self.assertIn(edge.column, child)
                self.assertIn(edge.parent_column, parent)
                self.assertEqual(parent[edge.parent_column], child[edge.column])

    def test_every_edge_is_many_to_one_onto_the_whole_parent_key(self):
        keys = self.document["keys"]["gold"]
        for edge in self.edges:
            self.assertEqual([edge.parent_column], keys[edge.parent])
            self.assertEqual("many-to-one", edge.cardinality)
            self.assertNotEqual([edge.column], keys[edge.child])

    def test_only_merchant_and_counterparty_are_optional(self):
        optional = {(edge.child, edge.column) for edge in self.edges if edge.optional}
        self.assertEqual({("fact_transactions", "merchant_id"),
                          ("fact_transactions", "counterparty_account_id")}, optional)

    def test_optionality_matches_the_fact_contract(self):
        contract = yaml.safe_load((ROOT / "data_platform/contracts/gold_fact_transactions.yml")
                                  .read_text(encoding="utf-8"))
        declared = {item["columns"][0]: item["nullable"] for item in contract["foreign_keys"]}
        drawn = {edge.column: edge.optional for edge in self.edges if edge.child == "fact_transactions"}
        self.assertEqual(declared, drawn)

    def test_dimension_to_dimension_edges_are_the_only_snowflake_links(self):
        links = {(edge.child, edge.parent) for edge in self.edges if edge.kind == "dimension-to-dimension"}
        self.assertEqual({("dim_account", "dim_user"), ("dim_device", "dim_user")}, links)
        # dim_user is also referenced directly by facts, so it is a conformed dimension
        # rather than a sub-dimension reachable only through another dimension.
        self.assertTrue({edge.child for edge in self.edges if edge.parent == "dim_user"} & set(ERD_FACTS))

    def test_an_edge_to_a_non_key_column_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["relationships"]["gold"][2]["references"] = "dim_user.email"
        with self.assertRaises(ValueError):
            gold_erd_edges(document)


class NamingConventionTest(unittest.TestCase):
    def setUp(self):
        self.observed, _ = inputs()

    def test_trino_schemas_are_the_configured_zone_schemas(self):
        self.assertEqual("delta", TRINO_CATALOG)
        self.assertEqual({"bronze": BRONZE_SCHEMA, "silver": SILVER_SCHEMA, "gold": GOLD_SCHEMA},
                         {zone: TRINO_SCHEMAS[zone] for zone in ("bronze", "silver", "gold")})
        self.assertEqual(("bronze_zone", "silver_zone", "gold_zone"), (BRONZE_SCHEMA, SILVER_SCHEMA, GOLD_SCHEMA))

    def test_bronze_and_silver_names_are_unprefixed_business_names(self):
        for zone in ("bronze", "silver"):
            for table in self.observed["zones"][zone]["tables"]:
                self.assertIsNone(re.match(r"(raw|stg|bronze|silver)_", table), table)

    def test_every_gold_table_carries_a_role_prefix(self):
        for table in GOLD_TABLES:
            self.assertNotEqual("Other", gold_role(table), table)


class TrinoSqlTest(unittest.TestCase):
    def setUp(self):
        self.observed, _ = inputs()

    def test_registration_only_writes_metadata(self):
        items = statements(render_trino_registration(self.observed))
        for item in items:
            self.assertRegex(item, r"^(CREATE SCHEMA IF NOT EXISTS|CALL delta\.system\.register_table\("
                                   r"|CREATE TABLE IF NOT EXISTS hive\.source_zone\.)")
            if item.startswith("CREATE TABLE"):
                self.assertIn("external_location = 'gs://", item)
        self.assertEqual(31, sum(item.startswith("CALL") for item in items))
        self.assertEqual(9, sum(item.startswith("CREATE TABLE") for item in items))

    def test_every_registered_location_is_an_observed_table(self):
        sql = render_trino_registration(self.observed)
        expected = {f"{zone['root']}/{table}" for zone in self.observed["zones"].values() for table in zone["tables"]}
        source = self.observed["source"]
        expected |= {f"{source['root']}/{table}" for table in source["tables"]}
        found = set(re.findall(r"location => '([^']+)'", sql)) | set(re.findall(r"external_location = '([^']+)'", sql))
        self.assertEqual(expected, found)

    def test_metadata_queries_are_read_only(self):
        for item in statements(render_trino_metadata_queries(self.observed)):
            self.assertRegex(item, r"^(SHOW (CATALOGS|SCHEMAS|TABLES)|SELECT|DESCRIBE)(\s|$)")
            self.assertNotRegex(item.upper(), r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|CALL|"
                                              r"OPTIMIZE|VACUUM|ANALYZE)\b")

    def test_metadata_queries_never_scan_table_data(self):
        for item in statements(render_trino_metadata_queries(self.observed)):
            if item.startswith("SELECT"):
                self.assertRegex(item, r"FROM \w+\.(information_schema\.\w+|\w+\.\"\w+\$history\")")

    def test_provenance_queries_only_show_table_definitions(self):
        items = statements(render_trino_provenance_queries(self.observed))
        self.assertEqual(5, len(items))
        for item in items:
            self.assertRegex(item, r"^SHOW CREATE TABLE (delta|hive)\.\w+_zone\.\w+$")


class ReadOnlyStackTest(unittest.TestCase):
    def test_only_the_registrar_may_write_and_only_register_table_is_callable(self):
        rules = json.loads((VIZ / "rules.json").read_text(encoding="utf-8"))
        writers = [rule for rule in rules["catalogs"] if rule["allow"] != "read-only"]
        self.assertEqual([{"user": "registrar", "catalog": "(delta|hive)", "allow": "all"}], writers)
        self.assertEqual([("registrar", "delta", "system", "register_table")],
                         [(rule["user"], rule["catalog"], rule["schema"], rule["procedure"])
                          for rule in rules["procedures"]])

    def test_the_stack_is_isolated_from_the_minio_trino(self):
        compose = yaml.safe_load((VIZ / "compose.yml").read_text(encoding="utf-8"))
        service = compose["services"]["trino"]
        self.assertEqual("edai1-schema-viz", compose["name"])
        self.assertEqual("edai1-schema-viz-trino", service["container_name"])
        self.assertEqual(["127.0.0.1:8091:8080"], service["ports"])
        self.assertTrue(any(volume.endswith("/var/run/gcp/adc.json:ro") for volume in service["volumes"]))

    def test_catalogs_read_gcs_with_a_local_metastore_and_no_inline_secret(self):
        for name in ("delta.properties", "hive.properties"):
            text = (VIZ / "catalog" / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertIn("fs.native-gcs.enabled=true", text)
                self.assertIn("hive.metastore.catalog.dir=local:///metastore/", text)
                self.assertNotRegex(text, r"(?i)minio|s3\.|aws|gcs\.json-key=|password")


class SourceExportTest(unittest.TestCase):
    @staticmethod
    def parquet_bytes() -> tuple[bytes, str]:
        table = pa.table({"user_id": pa.array(["u1"]), "kyc_verified": pa.array([True])})
        buffer = io.BytesIO()
        pq.write_table(table, buffer)
        data = buffer.getvalue()
        import hashlib

        return data, hashlib.sha256(str(pq.read_schema(pa.BufferReader(data))).encode()).hexdigest()

    def manifest(self, digests, status="COMPLETE"):
        return json.dumps({"dataset_version": "v1", "plan_digest": "p", "status": status, "tables": {
            "users": {"rows": 1, "bytes": 10, "schema_sha256": digests,
                      "files": [{"path": "users/shard-00001.parquet"}, {"path": "users/shard-00000.parquet"}]}}})

    def test_the_footer_gives_columns_and_the_generator_schema_hash(self):
        data, digest = self.parquet_bytes()
        found, found_digest = parse_parquet_footer(data[-4096:])
        self.assertEqual(digest, found_digest)
        self.assertEqual(["user_id", "kyc_verified"], [item["name"] for item in found])

    def test_a_truncated_or_foreign_tail_is_rejected(self):
        data, _ = self.parquet_bytes()
        with self.assertRaises(ValueError):
            parse_parquet_footer(data[-10:])
        with self.assertRaises(ValueError):
            parse_parquet_footer(b"not parquet at all")

    def test_export_reads_the_manifest_and_one_footer_per_dataset(self):
        data, digest = self.parquet_bytes()
        tails = []
        result = export_source("gs://b/source/v1", lambda uri: self.manifest([digest]),
                               lambda uri, size: tails.append(uri) or data[-size:])
        self.assertEqual(["gs://b/source/v1/users/shard-00000.parquet"], tails)
        self.assertEqual("users", result["tables"]["users"]["logical_table"])

    def test_export_rejects_a_footer_that_does_not_match_the_manifest(self):
        data, _ = self.parquet_bytes()
        with self.assertRaises(ValueError):
            export_source("gs://b/s", lambda uri: self.manifest(["0" * 64]), lambda uri, size: data)

    def test_export_rejects_mixed_schemas_and_incomplete_datasets(self):
        data, digest = self.parquet_bytes()
        with self.assertRaises(ValueError):
            export_source("gs://b/s", lambda uri: self.manifest([digest, "0" * 64]), lambda uri, size: data)
        with self.assertRaises(ValueError):
            export_source("gs://b/s", lambda uri: self.manifest([digest], "RUNNING"), lambda uri, size: data)


class DocumentationTest(unittest.TestCase):
    LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")

    def test_relative_links_resolve(self):
        for document in DOCS:
            for target in self.LINK.findall(document.read_text(encoding="utf-8")):
                if re.match(r"[a-z]+://", target):
                    continue
                with self.subTest(f"{document.name} -> {target}"):
                    self.assertTrue((document.parent / target).exists())

    def test_the_all_zones_inventory_numbers_match_the_observed_schemas(self):
        observed, _ = inputs()
        zones = observed["zones"]
        counted = {
            "Source": sum(len(item["columns"]) for item in observed["source"]["tables"].values()),
            "Bronze": sum(len(item["columns"]) for item in zones["bronze"]["tables"].values()),
            "Silver": sum(len(item["columns"]) for item in zones["silver"]["tables"].values()),
            "Gold": sum(len(item["columns"]) for zone in ("gold", "features")
                        for item in zones[zone]["tables"].values()),
        }
        text = (ROOT / "docs/edai1/schema/ALL_ZONES_SCHEMA.md").read_text(encoding="utf-8")
        for zone, total in counted.items():
            with self.subTest(zone):
                self.assertRegex(text, rf"\n\| {zone} \|[^\n]*\| {total} \|\n")
        self.assertIn(f"| **Total** | **39** | **40** | **{sum(counted.values())}** |", text)

    def test_no_pending_schema_screenshot_is_embedded(self):
        for document in DOCS:
            for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                if "screenshots/schema/" in target:
                    with self.subTest(target):
                        self.assertTrue((document.parent / target).exists())


if __name__ == "__main__":
    unittest.main()
