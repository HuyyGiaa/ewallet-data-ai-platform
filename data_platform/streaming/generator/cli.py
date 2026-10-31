"""Bounded deterministic stream-generator entrypoint."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator
from data_platform.streaming.messaging.redpanda import RedpandaConfig, RedpandaEventPublisher


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--bootstrap-servers", required=True)
    result.add_argument("--seed", type=int, required=True)
    result.add_argument("--count", type=int, required=True)
    result.add_argument("--rate", type=float, default=10.0)
    result.add_argument("--start-time", required=True)
    result.add_argument("--duplicate-every", type=int, default=0)
    result.add_argument("--out-of-order-every", type=int, default=0)
    result.add_argument("--late-every", type=int, default=0)
    result.add_argument("--invalid-every", type=int, default=0)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.count <= 0 or args.rate <= 0:
        raise SystemExit("count and rate must be positive")
    start = datetime.fromisoformat(args.start_time).astimezone(timezone.utc)
    generator = DeterministicScenarioGenerator(args.seed)
    publisher = RedpandaEventPublisher(RedpandaConfig(args.bootstrap_servers))
    interval = 1.0 / args.rate
    for sequence in range(args.count):
        arrival = start + timedelta(seconds=sequence)
        if args.invalid_every and sequence % args.invalid_every == 0:
            raw = json.dumps(generator.invalid_payload(sequence), sort_keys=True)
            publisher.publish_payload("ewallet.transactions.raw.v1", f"invalid-{sequence}", raw)
        else:
            event = generator.normal(sequence, arrival)
            if args.out_of_order_every and sequence % args.out_of_order_every == 0:
                event = generator.out_of_order(sequence, arrival, timedelta(minutes=5))
            if args.late_every and sequence % args.late_every == 0:
                event = generator.late(sequence, arrival, timedelta(hours=24))
            publisher.publish(event)
            if args.duplicate_every and sequence % args.duplicate_every == 0:
                publisher.publish(generator.duplicate(event, timedelta(seconds=1)))
        time.sleep(interval)
    if publisher.flush() != 0:
        raise RuntimeError("generator stopped with undelivered records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
