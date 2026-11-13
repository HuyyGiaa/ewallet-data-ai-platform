"""``psql`` adapter for the isolated benchmark container.

All SQL runs through ``docker exec <container> psql`` on the container's local
socket, so no database driver or host password is needed by the tooling. The
container is ``$EDAI1_WH_CONTAINER`` when set, else the benchmark's default container.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Sequence

from . import DATABASE, USER, container_name

Runner = Callable[..., subprocess.CompletedProcess]
_TIMING = re.compile(r"^Time: ([0-9.]+) ms", re.MULTILINE)


class PostgresError(RuntimeError):
    """A SQL statement failed."""


@dataclass
class Psql:
    """Run SQL in the benchmark database."""

    container: str = field(default_factory=container_name)
    runner: Runner = subprocess.run

    def _args(self, *extra: str) -> list[str]:
        return ["docker", "exec", "-i", self.container, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                "-U", USER, "-d", DATABASE, *extra]

    def run(self, sql: str, *, tuples: bool = True) -> str:
        """Execute ``sql``; return stdout (unaligned tuples by default)."""

        args = self._args("-A", "-t") if tuples else self._args()
        completed = self.runner(args, input=sql, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise PostgresError(f"psql failed ({completed.returncode}): {completed.stderr.strip()[:600]}")
        return completed.stdout.strip()

    def scalar(self, sql: str) -> str:
        """Single value of a one-row, one-column query."""

        output = self.run(sql)
        return output.splitlines()[-1] if output else ""

    def explain(self, sql: str, *, settings: Sequence[str] = ()) -> dict[str, Any]:
        """``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` of ``sql`` after optional ``SET`` lines."""

        prefix = "".join(f"{setting};\n" for setting in settings)
        output = self.run(f"{prefix}EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql};")
        return json.loads(output)[0]

    def timed(self, sql: str) -> float:
        """Execute ``sql`` with ``\\timing`` and return the server-reported milliseconds."""

        output = self.run(f"\\timing on\n{sql}\n", tuples=False)
        values = [float(value) for value in _TIMING.findall(output)]
        if not values:
            raise PostgresError("psql reported no timing")
        return sum(values)

    def copy_in(self, copy_command: str, stream: IO[bytes]) -> None:
        """Stream CSV bytes into ``\\copy ... FROM STDIN``."""

        completed = self.runner(self._args("-c", copy_command), stdin=stream, capture_output=True, check=False)
        if completed.returncode != 0:
            raise PostgresError(f"COPY failed: {completed.stderr.decode(errors='replace')[:600]}")


def plan_summary(explain: dict[str, Any]) -> dict[str, Any]:
    """Execution metrics and the access path of one EXPLAIN ANALYZE result."""

    nodes = []
    stack = [explain["Plan"]]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(node.get("Plans") or [])
    scans = [node for node in nodes if "Scan" in node["Node Type"]]
    root = explain["Plan"]
    return {
        "planning_ms": explain["Planning Time"],
        "execution_ms": explain["Execution Time"],
        "root_node": root["Node Type"],
        "access_paths": sorted({f"{node['Node Type']}:{node.get('Index Name', node.get('Relation Name', ''))}"
                                for node in scans}),
        "shared_hit_blocks": root.get("Shared Hit Blocks", 0),
        "shared_read_blocks": root.get("Shared Read Blocks", 0),
        "heap_fetches": sum(int(node.get("Heap Fetches", 0)) for node in scans),
        "rows_removed_by_filter": sum(int(node.get("Rows Removed by Filter", 0)) for node in scans),
    }
