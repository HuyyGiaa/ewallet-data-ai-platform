"""The native DBeaver diagrams stay tied to real tables, contracts and validator rules."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import yaml

from data_platform.storage.scripts.config import SILVER_SCHEMA, GOLD_SCHEMA, TRINO_CATALOG
from ops.schema import dbeaver_erd
from ops.schema.dbeaver_erd import (
    DIAGRAMS,
    GOLD,
    SILVER,
    committed_connection_id,
    diagram_edges,
    find_connection_id,
    install,
    load_validators,
    render_all,
    validator_edges,
)
from ops.schema.render_schema_diagrams import RELATIONSHIPS, load_relationships


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / dbeaver_erd.OUTPUT_DIR
OBSERVED = json.loads((ROOT / "docs/edai1/schema/generated/observed_schemas.json").read_text(encoding="utf-8"))
CONTRACT = yaml.safe_load((ROOT / RELATIONSHIPS).read_text(encoding="utf-8"))
VALIDATORS = load_validators((ROOT / dbeaver_erd.SILVER_VALIDATOR, ROOT / dbeaver_erd.GOLD_VALIDATOR))


def edges(diagram):
    return diagram_edges(diagram, CONTRACT, OBSERVED, VALIDATORS)


class DiagramTablesTest(unittest.TestCase):
    def test_silver_holds_every_silver_table_once(self):
        tables = [box.table for box in SILVER.boxes]
        self.assertEqual(len(tables), 8)
        self.assertEqual(sorted(tables), sorted(OBSERVED["zones"]["silver"]["tables"]))
        self.assertEqual(SILVER.schema, SILVER_SCHEMA)

    def test_gold_holds_every_gold_and_feature_table_once(self):
        tables = [box.table for box in GOLD.boxes]
        expected = list(OBSERVED["zones"]["gold"]["tables"]) + list(OBSERVED["zones"]["features"]["tables"])
        self.assertEqual(len(tables), 15)
        self.assertEqual(sorted(tables), sorted(expected))
        self.assertEqual(GOLD.schema, GOLD_SCHEMA)
        for box in GOLD.boxes:
            self.assertIn(box.table, OBSERVED["zones"][box.zone]["tables"])

    def test_boxes_do_not_overlap_at_generous_sizes(self):
        for diagram in DIAGRAMS:
            rects = []
            for box in diagram.boxes:
                columns = OBSERVED["zones"][box.zone]["tables"][box.table]["columns"]
                width = 9 * max(len(item["name"]) + len(item["type"]) + 2 for item in columns) + 60
                rects.append((box.table, box.x, box.y, box.x + width, box.y + 40 + 24 * len(columns)))
            rects += [(note.text[:20], note.x, note.y, note.x + note.w, note.y + note.h) for note in diagram.notes]
            for index, first in enumerate(rects):
                for second in rects[index + 1:]:
                    overlap = first[1] < second[3] and second[1] < first[3] and first[2] < second[4] and second[2] < first[4]
                    self.assertFalse(overlap, f"{diagram.name}: {first[0]} overlaps {second[0]}")


class RelationshipTest(unittest.TestCase):
    def test_validator_rules_are_read_from_source(self):
        silver = validator_edges((ROOT / dbeaver_erd.SILVER_VALIDATOR).read_text(encoding="utf-8"))
        gold = validator_edges((ROOT / dbeaver_erd.GOLD_VALIDATOR).read_text(encoding="utf-8"))
        self.assertEqual(len(silver), 11)
        self.assertEqual(silver[("fraud_labels", "transaction_id", "transactions", "transaction_id")],
                         "fraud_labels.transaction_fk")
        self.assertEqual(len(gold), 19)
        self.assertEqual(gold[("fact_transactions", "date_key", "dim_date", "date_key")],
                         "fact_transactions.date_key_exists_in_dim_date")

    def test_silver_draws_all_eleven_candidates(self):
        drawn, unverified = edges(SILVER)
        self.assertEqual(len(drawn), 11)
        self.assertEqual(unverified, [])
        optional = {(edge.child, edge.column) for edge in drawn if edge.nullable}
        self.assertEqual(optional, {("transactions", "merchant_id"), ("transactions", "counterparty_account_id")})

    def test_gold_draws_validated_edges_and_leaves_lineage_out(self):
        drawn, unverified = edges(GOLD)
        self.assertEqual(len(drawn), 19)
        dimensional = [edge for edge in drawn if edge.child.startswith(("dim_", "fact_"))]
        self.assertEqual(len(dimensional), 13)
        self.assertEqual(unverified, [("obt_transaction_enriched", "transaction_id", "fact_transactions",
                                       "transaction_id", "builder_only")])
        outriggers = {(edge.child, edge.parent) for edge in drawn if edge.group.startswith("dimension")}
        self.assertEqual(outriggers, {("dim_account", "dim_user"), ("dim_device", "dim_user")})

    def test_every_contract_edge_is_drawn_or_reported(self):
        for diagram in DIAGRAMS:
            tables = {box.table for box in diagram.boxes}
            contract = [item for item in load_relationships(CONTRACT)
                        if item.table in tables and item.target_table in tables]
            drawn, unverified = edges(diagram)
            self.assertEqual(len(contract), len(drawn) + len(unverified))

    def test_every_edge_targets_a_whole_key_of_the_same_type(self):
        for diagram in DIAGRAMS:
            zones = {box.table: box.zone for box in diagram.boxes}
            for edge in edges(diagram)[0]:
                types = [{item["name"]: item["type"]
                          for item in OBSERVED["zones"][zones[table]]["tables"][table]["columns"]}[column]
                         for table, column in ((edge.child, edge.column), (edge.parent, edge.parent_column))]
                self.assertEqual(types[0], types[1], edge)

    def test_an_unchecked_contract_edge_is_not_drawn(self):
        validators = dict(VALIDATORS)
        validators.pop(("transactions", "merchant_id", "merchants", "merchant_id"))
        drawn, unverified = diagram_edges(SILVER, CONTRACT, OBSERVED, validators)
        self.assertEqual(len(drawn), 10)
        self.assertEqual(unverified[0][:2], ("transactions", "merchant_id"))

    def test_a_validator_edge_outside_the_contract_fails(self):
        validators = dict(VALIDATORS)
        validators[("users", "user_id", "accounts", "account_id")] = "invented"
        with self.assertRaises(ValueError):
            diagram_edges(SILVER, CONTRACT, OBSERVED, validators)


class ErdFileTest(unittest.TestCase):
    def test_committed_files_match_their_inputs(self):
        rendered = render_all(OBSERVED, CONTRACT, VALIDATORS, committed_connection_id(OUTPUT))
        for name, content in rendered.items():
            self.assertEqual((OUTPUT / name).read_text(encoding="utf-8"), content, name)

    def test_erd_layout_is_what_dbeaver_loader_reads(self):
        for diagram in DIAGRAMS:
            root = ElementTree.parse(OUTPUT / f"{diagram.name}.erd").getroot()
            self.assertEqual(root.tag, "diagram")
            self.assertEqual(root.attrib["version"], "1")
            self.assertEqual(root.attrib["name"], diagram.name)
            sources = root.findall("entities/data-source")
            self.assertEqual(len(sources), 1)
            entities = sources[0].findall("entity")
            self.assertEqual([item.attrib["name"] for item in entities], [box.table for box in diagram.boxes])
            for entity in entities:
                # The loader prepends each path, so the innermost container comes first.
                self.assertEqual([item.attrib["name"] for item in entity.findall("path")],
                                 [diagram.schema, TRINO_CATALOG])
                int(entity.attrib["x"]), int(entity.attrib["y"])
                self.assertEqual(entity.attrib["showAttrs"], "ALL")
            ids = [item.attrib["id"] for item in root.iter() if "id" in item.attrib and item.tag != "data-source"]
            self.assertEqual(len(ids), len(set(ids)))
            for note in root.findall("notes/note"):
                self.assertTrue(all(int(note.attrib[key]) > 0 for key in ("w", "h")))
            self.assertEqual(list(root.find("relations")), [])

    def test_notes_say_the_lines_are_virtual(self):
        for diagram in DIAGRAMS:
            text = " ".join(note.text for note in diagram.notes)
            self.assertIn("VIRTUAL", text)
            self.assertIn("no constraint exists", text)


class WorkspaceTest(unittest.TestCase):
    def make_workspace(self, root: Path, port: str = "8091") -> Path:
        project = root / "General"
        (project / ".dbeaver").mkdir(parents=True)
        (project / ".project").write_text("<projectDescription/>", encoding="utf-8")
        registry = {"connections": {
            "old": {"name": "delta", "configuration": {"port": "8081"}},
            committed_connection_id(OUTPUT): {"name": dbeaver_erd.CONNECTION_NAME,
                                              "configuration": {"port": port}},
        }}
        (project / ".dbeaver" / "data-sources.json").write_text(json.dumps(registry), encoding="utf-8")
        return root

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_install_creates_new_files_and_never_touches_dbeaver_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            registry = workspace / "General/.dbeaver/data-sources.json"
            before = self.digest(registry)
            report = install(OUTPUT, workspace, "General")
            self.assertEqual(len([line for line in report if line.startswith("created")]), 2)
            self.assertEqual(install(OUTPUT, workspace, "General")[0].split()[0], "unchanged")
            self.assertEqual(self.digest(registry), before)
            self.assertEqual(dbeaver_erd.check(OUTPUT, workspace, "General"), [])

    def test_install_refuses_to_replace_a_different_diagram(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            existing = workspace / "General/Diagrams/EDAI1_Gold_ERD.erd"
            existing.parent.mkdir()
            existing.write_text("owner diagram", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                install(OUTPUT, workspace, "General")
            self.assertEqual(existing.read_text(encoding="utf-8"), "owner diagram")
            self.assertFalse((workspace / "General/Diagrams/EDAI1_Silver_ERD.erd").exists())

    def test_connection_is_found_by_name_and_must_be_the_task_trino(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary))
            self.assertEqual(find_connection_id(workspace, "General"), committed_connection_id(OUTPUT))
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary), port="8081")
            with self.assertRaises(ValueError):
                find_connection_id(workspace, "General")


if __name__ == "__main__":
    unittest.main()
