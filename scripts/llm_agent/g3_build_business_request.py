"""Build a canonical G3 A2A request from a reviewed business-request JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_platform.llm_agent.business_request import G3BusinessRequest, a2a_message_send


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = G3BusinessRequest.model_validate_json(
        args.business_request.read_text(encoding="utf-8")
    )
    payload = a2a_message_send(request)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(request.to_compact_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
