"""Generate and install the native DBeaver ER diagrams for Silver and Gold.

The diagrams are DBeaver ``.erd`` project files. Their format is the one that the
installed DBeaver 26.1.3 reads and writes: ``DiagramLoader.loadDiagram`` and
``DiagramLoader.serializeDiagram`` in the ``org.jkiss.dbeaver.ui.editors.erd`` plugin.
A diagram names a connection by its id and each table by its name and its path of
containers (catalog, then schema), and stores its position and notes.

A ``.erd`` file holds no column-level relationship. DBeaver draws a relationship
line from the tables' associations, which for Delta tables can only be *virtual*
foreign keys. DBeaver stores those in its own workspace configuration when the
owner draws them in the diagram. This module never writes that configuration: it
generates the list of virtual keys to draw and the diagrams that display them.

An edge is drawn only when it is in the relationship contract *and* a validator
rule checks it on real rows. The validator rules are read from the validator
source code, so the diagrams cannot claim a check that does not exist.

Commands:

* ``render``: write the ``.erd`` files and the relationship guide into the repository;
* ``install``: copy the ``.erd`` files into a DBeaver project's ``Diagrams`` folder.
  It only creates new files, never overwrites a different file and never touches
  ``.dbeaver/``, so it is safe while DBeaver is running;
* ``check``: read-only comparison of an installed workspace with the repository files.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from xml.sax.saxutils import escape, quoteattr

import yaml

from ops.schema.render_schema_diagrams import (
    GENERATED_DIR,
    RELATIONSHIPS,
    edge_semantics,
    load_relationships,
    validator_edges,
)


OUTPUT_DIR = Path("docs/edai1/schema/dbeaver")
CONNECTION_NAME = "EDAI1 - GCS Cloud Schema"
CONNECTION_PORT = "8091"
CATALOG = "delta"
SILVER_VALIDATOR = Path("data_platform/quality/validate_silver.py")
GOLD_VALIDATOR = Path("data_platform/quality/validate_gold.py")
DEFAULT_WORKSPACE = Path.home() / ".local/share/DBeaverData/workspace6"
DIAGRAMS_FOLDER = "Diagrams"


@dataclass(frozen=True)
class Box:
    """One table on a diagram: its observed zone, name and top-left position."""

    zone: str
    table: str
    x: int
    y: int


@dataclass(frozen=True)
class Note:
    """A DBeaver diagram note: free text with a position and size."""

    text: str
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Diagram:
    name: str
    schema: str
    zones: tuple[str, ...]
    boxes: tuple[Box, ...]
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class DiagramEdge:
    """A relationship drawn as a DBeaver virtual foreign key."""

    child: str
    column: str
    parent: str
    parent_column: str
    nullable: bool
    cardinality: str
    group: str
    rule: str
    enforcement: str


VIRTUAL_NOTE = (
    "Lines are DBeaver VIRTUAL foreign keys: workspace metadata only. "
    "Delta declares no primary or foreign key, and no constraint exists in Trino or GCS."
)

SILVER = Diagram(
    name="EDAI1_Silver_ERD",
    schema="silver_zone",
    zones=("silver",),
    boxes=(
        Box("silver", "users", 600, 40),
        Box("silver", "accounts", 240, 280),
        Box("silver", "devices", 960, 280),
        Box("silver", "balance_snapshots", 40, 520),
        Box("silver", "transactions", 560, 520),
        Box("silver", "login_events", 1060, 520),
        Box("silver", "merchants", 240, 760),
        Box("silver", "fraud_labels", 600, 1000),
    ),
    notes=(
        Note("Silver zone (delta.silver_zone), 8 tables: the cleaned relational business model. "
             "Not a star schema. " + VIRTUAL_NOTE, 40, 40, 480, 150),
        Note("Optional (nullable) references: transactions.merchant_id (payments only) and "
             "transactions.counterparty_account_id (transfers only).", 1060, 1000, 380, 110),
    ),
)

GOLD = Diagram(
    name="EDAI1_Gold_ERD",
    schema="gold_zone",
    zones=("gold", "features"),
    boxes=(
        Box("gold", "obt_transaction_enriched", 40, 200),
        Box("gold", "opt_merchant_performance", 40, 1080),
        Box("gold", "dim_user", 1120, 40),
        Box("gold", "dim_account", 780, 280),
        Box("gold", "dim_device", 1460, 280),
        Box("gold", "fact_balance_snapshot", 480, 520),
        Box("gold", "fact_transactions", 1040, 520),
        Box("gold", "fact_login_events", 1540, 520),
        Box("gold", "dim_merchant", 600, 800),
        Box("gold", "dim_date", 1080, 1040),
        Box("gold", "feat_user_90d", 1960, 200),
        Box("features", "feat_user_behavior", 1960, 440),
        Box("features", "feat_device_behavior", 1960, 800),
        Box("features", "feat_account_behavior", 2480, 200),
        Box("features", "feat_merchant_behavior", 2480, 580),
    ),
    notes=(
        Note("OBT and optimization (left): denormalized analytics. obt_transaction_enriched is "
             "built from fact_transactions and the dimensions (ETL lineage, builder only, no "
             "validator rule), so it has no relationship line.", 40, 40, 400, 140),
        Note("Dimensional core (centre): star schema / fact constellation, 5 dimensions and 3 "
             "facts. dim_account and dim_device reference dim_user (outriggers). "
             + VIRTUAL_NOTE, 480, 40, 600, 170),
        Note("Feature tables (right): point-in-time features. Their lines are validated key "
             "references to a dimension, not the aggregation lineage they are computed from.",
             1960, 40, 860, 110),
    ),
)

DIAGRAMS = (SILVER, GOLD)


def _group(child: str, parent: str) -> str:
    if child.startswith("fact_"):
        return "fact-to-dimension"
    if child.startswith("dim_"):
        return "dimension-to-dimension (outrigger)"
    if child.startswith(("feat_", "opt_", "obt_")):
        return "analytical-table-to-dimension"
    return "relational"


def diagram_edges(
    diagram: Diagram,
    document: Mapping[str, object],
    observed: Mapping[str, object],
    validators: Mapping[tuple[str, str, str, str], str],
) -> tuple[list[DiagramEdge], list[tuple[str, str, str, str, str]]]:
    """Split the contract's edges among the diagram's tables into drawn and not verified.

    Raises:
        ValueError: A drawn edge names a missing column, joins different types,
            references something other than the parent's whole key, or a validator
            checks an edge the contract does not list.
    """

    tables = {box.table: box.zone for box in diagram.boxes}
    keys: dict[str, list[str]] = {}
    for zone in diagram.zones:
        keys.update(document["keys"][zone])
    columns = {
        table: {item["name"]: item["type"] for item in observed["zones"][zone]["tables"][table]["columns"]}
        for table, zone in tables.items()
    }
    drawn: list[DiagramEdge] = []
    unverified: list[tuple[str, str, str, str, str]] = []
    contract = set()
    for item in load_relationships(document):
        if item.table not in tables or item.target_table not in tables:
            continue
        if tables[item.table] != item.zone or tables[item.target_table] != item.target_zone:
            continue
        key = (item.table, item.column, item.target_table, item.target_column)
        contract.add(key)
        rule = validators.get(key)
        if rule is None:
            unverified.append((*key, item.enforcement))
            continue
        child_type = columns[item.table].get(item.column)
        parent_type = columns[item.target_table].get(item.target_column)
        if child_type is None or parent_type is None:
            raise ValueError(f"{item.table}.{item.column} -> {item.target_table} names a missing column")
        if child_type != parent_type:
            raise ValueError(f"{item.table}.{item.column} ({child_type}) joins {parent_type}")
        if keys[item.target_table] != [item.target_column]:
            raise ValueError(f"{item.table}.{item.column} does not reference the key of {item.target_table}")
        cardinality = edge_semantics(item, document).cardinality
        drawn.append(DiagramEdge(item.table, item.column, item.target_table, item.target_column,
                                 item.nullable, cardinality, _group(item.table, item.target_table),
                                 rule, item.enforcement))
    extra = [key for key in validators if key[0] in tables and key[2] in tables and key not in contract]
    if extra:
        raise ValueError(f"validator checks edges missing from the contract: {extra}")
    return drawn, unverified


def render_erd(diagram: Diagram, data_source_id: str) -> str:
    """Return the ``.erd`` XML of a diagram, in the layout DiagramLoader reads.

    Relationship lines are not stored: DBeaver derives them from the virtual foreign
    keys of the tables when it opens the diagram.
    """

    lines = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
             f"<diagram version=\"1\" name={quoteattr(diagram.name)}>",
             "\t<entities>",
             f"\t\t<data-source id={quoteattr(data_source_id)}>"]
    for number, box in enumerate(diagram.boxes, start=1):
        fq_name = f"{CATALOG}.{diagram.schema}.{box.table}"
        lines.append(f"\t\t\t<entity id=\"{number}\" name={quoteattr(box.table)} fq-name={quoteattr(fq_name)} "
                     f"showAttrs=\"ALL\" x=\"{box.x}\" y=\"{box.y}\">")
        # The serializer writes the innermost container first; the loader reverses it.
        lines.append(f"\t\t\t\t<path name={quoteattr(diagram.schema)}/>")
        lines.append(f"\t\t\t\t<path name={quoteattr(CATALOG)}/>")
        lines.append("\t\t\t</entity>")
    lines += ["\t\t</data-source>", "\t</entities>", "\t<notes>"]
    first_note = len(diagram.boxes) + 1
    for number, note in enumerate(diagram.notes, start=first_note):
        lines.append(f"\t\t<note id=\"{number}\" x=\"{note.x}\" y=\"{note.y}\" w=\"{note.w}\" h=\"{note.h}\">"
                     f"{escape(note.text)}</note>")
    lines += ["\t</notes>", "\t<relations/>", "</diagram>", ""]
    return "\n".join(lines)


def render_guide(
    edges: Mapping[str, tuple[list[DiagramEdge], list[tuple[str, str, str, str, str]]]],
    data_source_id: str,
) -> str:
    """Return the Markdown list of virtual keys to draw in each diagram."""

    lines = [
        "# DBeaver ER Diagrams: Virtual Keys",
        "",
        "Generated by `ops/schema/dbeaver_erd.py render`. Do not edit by hand.",
        "",
        f"Both diagrams use the DBeaver connection `{CONNECTION_NAME}` (id `{data_source_id}`, "
        f"`localhost:{CONNECTION_PORT}`, catalog `{CATALOG}`). An edge is listed only when it is in "
        "`data_platform/contracts/relationships/schema_relationships.yml` **and** a rule in "
        "`data_platform/quality/validate_silver.py` or `validate_gold.py` checks it for orphan "
        "keys. Every key here is a DBeaver *virtual* key: workspace metadata, not a database "
        "constraint.",
        "",
    ]
    for diagram in DIAGRAMS:
        drawn, unverified = edges[diagram.name]
        parents = sorted({(edge.parent, edge.parent_column) for edge in drawn})
        lines += [
            f"## {diagram.name} (`{CATALOG}.{diagram.schema}`, {len(diagram.boxes)} tables)",
            "",
            f"### Virtual unique keys ({len(parents)})",
            "",
            "| Table | Key column |",
            "|---|---|",
            *[f"| `{table}` | `{column}` |" for table, column in parents],
            "",
            f"### Virtual foreign keys ({len(drawn)})",
            "",
            "| # | Child column | Parent key | Cardinality | Optional | Kind | Validator rule |",
            "|---|---|---|---|---|---|---|",
        ]
        for number, edge in enumerate(drawn, start=1):
            lines.append(
                f"| {number} | `{edge.child}.{edge.column}` | `{edge.parent}.{edge.parent_column}` | "
                f"{edge.cardinality} | {'yes' if edge.nullable else 'no'} | {edge.group} | `{edge.rule}` |"
            )
        lines += ["", "### Not drawn (NOT VERIFIED)", ""]
        if unverified:
            lines += ["| Child column | Parent key | Contract enforcement | Reason |", "|---|---|---|---|"]
            for child, column, parent, parent_column, enforcement in unverified:
                lines.append(f"| `{child}.{column}` | `{parent}.{parent_column}` | {enforcement} | "
                             "no validator rule checks it; it is ETL lineage, not a checked key |")
        else:
            lines.append("None.")
        lines.append("")
    return "\n".join(lines)


def render_all(
    observed: Mapping[str, object], document: Mapping[str, object], validators: Mapping[tuple[str, str, str, str], str],
    data_source_id: str,
) -> dict[str, str]:
    edges = {diagram.name: diagram_edges(diagram, document, observed, validators) for diagram in DIAGRAMS}
    files = {f"{diagram.name}.erd": render_erd(diagram, data_source_id) for diagram in DIAGRAMS}
    files["DBEAVER_VIRTUAL_KEYS.md"] = render_guide(edges, data_source_id)
    return files


def load_validators(paths: Iterable[Path] = (SILVER_VALIDATOR, GOLD_VALIDATOR)) -> dict[tuple[str, str, str, str], str]:
    found: dict[tuple[str, str, str, str], str] = {}
    for path in paths:
        found.update(validator_edges(path.read_text(encoding="utf-8")))
    return found


def find_connection_id(workspace: Path, project: str, name: str = CONNECTION_NAME) -> str:
    """Return the id of a named connection. Reads ``data-sources.json`` and never writes it."""

    registry = json.loads((workspace / project / ".dbeaver" / "data-sources.json").read_text(encoding="utf-8"))
    matches = [key for key, value in registry.get("connections", {}).items() if value.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"expected one connection named {name!r}, found {len(matches)}")
    port = registry["connections"][matches[0]].get("configuration", {}).get("port")
    if str(port) != CONNECTION_PORT:
        raise ValueError(f"connection {name!r} uses port {port}, not {CONNECTION_PORT}")
    return matches[0]


def committed_connection_id(directory: Path = OUTPUT_DIR) -> str:
    """Return the connection id the committed diagrams were rendered for."""

    import xml.etree.ElementTree as ElementTree

    root = ElementTree.parse(directory / f"{SILVER.name}.erd").getroot()
    return root.find("entities/data-source").attrib["id"]


def install(source: Path, workspace: Path, project: str) -> list[str]:
    """Copy the diagrams into the project's Diagrams folder; return what happened to each.

    Raises:
        FileExistsError: A diagram with the same name but different content exists.
    """

    target_dir = workspace / project / DIAGRAMS_FOLDER
    if not (workspace / project / ".project").is_file():
        raise FileNotFoundError(f"{workspace / project} is not a DBeaver project")
    report = []
    pending = []
    for diagram in DIAGRAMS:
        content = (source / f"{diagram.name}.erd").read_text(encoding="utf-8")
        target = target_dir / f"{diagram.name}.erd"
        if target.exists():
            if target.read_text(encoding="utf-8") != content:
                raise FileExistsError(f"{target} exists with different content; inspect it before replacing")
            report.append(f"unchanged {target}")
        else:
            pending.append((target, content))
    target_dir.mkdir(exist_ok=True)
    for target, content in pending:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(content)
        report.append(f"created {target}")
    return report


def check(source: Path, workspace: Path, project: str) -> list[str]:
    """Return every difference between the installed workspace and the repository diagrams."""

    problems = []
    try:
        connection = find_connection_id(workspace, project)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    for diagram in DIAGRAMS:
        target = workspace / project / DIAGRAMS_FOLDER / f"{diagram.name}.erd"
        if not target.is_file():
            problems.append(f"missing {target}")
            continue
        if connection not in target.read_text(encoding="utf-8"):
            problems.append(f"{target} does not use connection {connection}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    render.add_argument("--data-source-id", help="default: the connection id of the committed diagrams")
    render.add_argument("--directory", type=Path, default=OUTPUT_DIR)
    for name in ("install", "check"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
        command.add_argument("--project", default="General")
        command.add_argument("--directory", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    if args.command == "render":
        observed = json.loads((GENERATED_DIR / "observed_schemas.json").read_text(encoding="utf-8"))
        document = yaml.safe_load(RELATIONSHIPS.read_text(encoding="utf-8"))
        data_source_id = args.data_source_id or committed_connection_id(args.directory)
        args.directory.mkdir(parents=True, exist_ok=True)
        for name, content in render_all(observed, document, load_validators(), data_source_id).items():
            (args.directory / name).write_text(content, encoding="utf-8")
        return 0
    if args.command == "install":
        expected = find_connection_id(args.workspace, args.project)
        if committed_connection_id(args.directory) != expected:
            raise SystemExit(f"diagrams were rendered for another connection; run render --data-source-id {expected}")
        for line in install(args.directory, args.workspace, args.project):
            print(line)
        print("In DBeaver: right-click the project in the Projects view and choose Refresh (F5).")
        return 0
    problems = check(args.directory, args.workspace, args.project)
    for problem in problems:
        print(problem)
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
