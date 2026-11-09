"""Live, read-only view of a running benchmark session, for a second terminal.

``vllm_bench.sh`` sends the clients' output to files, so the session terminal is quiet
while a run is measured. This follows what the session itself writes:

* new lines of ``session.log`` (run started, pods ready, policy probe, cleanup);
* every request the client records in ``<arm>/raw/run-NN/raw_requests.jsonl``, as it
  finishes: level, request id, category, HTTP status, TTFT, E2E, output tokens, backend.

It only reads local files: no cluster or gateway call, no credentials (the raw records hold
no prompts or keys), so it cannot change what is measured.

usage: python -m ops.experiments.progress <session evidence dir> [--once] [--interval 2]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Sequence


def request_line(arm: str, run: str, row: dict[str, Any]) -> str:
    """One finished request."""

    def ms(value: Any) -> str:
        return f"{value:8.0f}" if isinstance(value, (int, float)) else "     n/a"

    stamp = time.strftime("%H:%M:%SZ", time.gmtime(row["started_at"])) if row.get("started_at") else "        "
    state = "FAILED" if row.get("failed") else "ok"
    return (f"{stamp} {arm:<9} run {run} c={row.get('level', '?'):<2} {str(row.get('id')):<14}"
            f" {str(row.get('category') or ''):<12} http {row.get('status')} ttft_ms {ms(row.get('ttft_ms'))}"
            f" e2e_ms {ms(row.get('e2e_ms'))} out_tok {row.get('output_tokens', 'n/a'):>4}"
            f" {row.get('backend') or '-':<7} {state}")


class Follower:
    """Remembers how far each file was read, so every line is printed once."""

    def __init__(self, session: Path) -> None:
        self.session = session
        self.offsets: dict[Path, int] = {}

    def _new_lines(self, path: Path) -> list[str]:
        try:
            with path.open("rb") as handle:
                handle.seek(self.offsets.get(path, 0))
                data = handle.read()
        except OSError:
            return []
        complete = data[:data.rfind(b"\n") + 1]          # a line still being written waits
        self.offsets[path] = self.offsets.get(path, 0) + len(complete)
        return complete.decode("utf-8", errors="replace").splitlines()

    def poll(self) -> list[str]:
        out = [f"[session] {line}" for line in self._new_lines(self.session / "session.log")
               if line[:4].isdigit()]                     # timestamped runner lines only
        for raw in sorted(self.session.glob("*/raw/run-*/raw_requests.jsonl")):
            arm, run = raw.parts[-4], raw.parts[-2].removeprefix("run-")
            for line in self._new_lines(raw):
                try:
                    out.append(request_line(arm, run, json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue
        return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", type=Path, help="the evidence directory given to vllm_bench.sh session")
    parser.add_argument("--once", action="store_true", help="print what is there and exit")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not args.session.is_dir():
        parser.error(f"{args.session} does not exist yet; start the session first")
    follower = Follower(args.session)
    print(f"following {args.session} (read-only; Ctrl-C to stop)", flush=True)
    try:
        while True:
            for line in follower.poll():
                print(line, flush=True)
            if args.once or (args.session / "manifest.json").exists() and _finished(args.session):
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _finished(session: Path) -> bool:
    try:
        return bool(json.loads((session / "manifest.json").read_text(encoding="utf-8")).get("finished_at"))
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
