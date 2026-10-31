"""Dependency-free bounded sample entrypoint for later broker benchmarks."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from data_platform.streaming.generator.scenarios import (
    DeterministicScenarioGenerator,
    GeneratorProfile,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--count", type=int, required=True)
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--start-time", required=True)
    result.add_argument("--rate", type=float, default=10.0)
    result.add_argument("--stress", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    profile = GeneratorProfile(seed=args.seed, realistic_events_per_second=args.rate)
    rate = profile.emission_rate(stress=args.stress)
    generator = DeterministicScenarioGenerator(seed=args.seed, profile=profile)
    start = datetime.fromisoformat(args.start_time)
    for event in generator.sample(
        args.count,
        start=start,
        interval=timedelta(seconds=1 / rate),
    ):
        print(event.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
