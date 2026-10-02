"""
Flink Burst Traffic Monitor

Measure incoming transaction rate using
10-second processing-time windows.

This is an operational monitoring branch,
not the business Event-Time feature pipeline.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pyflink.common import Types
from pyflink.common.time import Time
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import FlinkKafkaConsumer
from pyflink.datastream.functions import (
    AggregateFunction,
    ProcessAllWindowFunction,
)
from pyflink.datastream.window import (
    TumblingProcessingTimeWindows,
)


# ============================================================
# Config
# ============================================================

BOOTSTRAP_SERVERS = "localhost:9092"
KAFKA_TOPIC = "transactions.raw"
CONSUMER_GROUP = "flink-burst-monitor"

BURST_WINDOW_SECONDS = 10

# Current generator normal rate:
#     ~50 events/minute
#
# Planned burst:
#     x30
#
# Normal ≈ 8 events / 10 sec
# Burst  ≈ 250 events / 10 sec
#
# 100 therefore gives a visible demo threshold
# between the two workloads.
DEFAULT_BURST_THRESHOLD = 100

KAFKA_JAR = (
    Path(__file__).resolve().parent
    / "jars"
    / "flink-sql-connector-kafka-3.3.0-1.19.jar"
)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# ============================================================
# Aggregation
# ============================================================

class EventCountAggregate(
    AggregateFunction
):

    def create_accumulator(
        self,
    ):

        return 0

    def add(
        self,
        value,
        accumulator,
    ):

        return accumulator + 1

    def get_result(
        self,
        accumulator,
    ):

        return accumulator

    def merge(
        self,
        first,
        second,
    ):

        return (
            first + second
        )


class BurstWindowResult(
    ProcessAllWindowFunction
):

    def __init__(
        self,
        threshold: int,
    ):

        self.threshold = threshold

    def process(
        self,
        context,
        aggregates,
    ):

        event_count = next(
            iter(
                aggregates
            )
        )

        burst_detected = (
            event_count
            >= self.threshold
        )

        yield (
            context.window().start,
            context.window().end,
            event_count,
            burst_detected,
        )


BURST_RESULT_TYPE = Types.TUPLE(
    [
        Types.LONG(),
        Types.LONG(),
        Types.LONG(),
        Types.BOOLEAN(),
    ]
)


# ============================================================
# Pipeline
# ============================================================

def run_monitor(
    threshold: int,
    parallelism: int,
) -> None:

    if not KAFKA_JAR.exists():
        raise FileNotFoundError(
            f"Kafka connector JAR not found: {KAFKA_JAR}"
        )

    logger.info(
        "========== FLINK BURST MONITOR =========="
    )

    logger.info(
        "Window: %s seconds",
        BURST_WINDOW_SECONDS,
    )

    logger.info(
        "Burst threshold: %s events/window",
        threshold,
    )

    env = (
        StreamExecutionEnvironment
        .get_execution_environment()
    )

    env.set_parallelism(
        parallelism
    )

    env.add_jars(
        KAFKA_JAR.as_uri()
    )

    consumer = FlinkKafkaConsumer(
        topics=KAFKA_TOPIC,
        deserialization_schema=(
            SimpleStringSchema()
        ),
        properties={
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": CONSUMER_GROUP,
        },
    )

    consumer.set_start_from_latest()

    raw_stream = (
        env
        .add_source(
            consumer
        )
        .name(
            "transactions-raw-source"
        )
    )

    burst_metrics = (
        raw_stream

        .window_all(
            TumblingProcessingTimeWindows.of(
                Time.seconds(BURST_WINDOW_SECONDS)
            )
        )

        .aggregate(
            EventCountAggregate(),

            window_function=(
                BurstWindowResult(
                    threshold
                )
            ),

            accumulator_type=(
                Types.LONG()
            ),

            output_type=(
                BURST_RESULT_TYPE
            ),
        )

        .name(
            "burst-rate-monitor"
        )
    )

    burst_metrics.print(
        "BURST"
    )

    env.execute(
        "EWallet-Flink-Burst-Monitor"
    )


# ============================================================
# CLI
# ============================================================

def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            "Monitor Redpanda burst traffic with PyFlink."
        )
    )

    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_BURST_THRESHOLD,
    )

    parser.add_argument(
        "--parallelism",
        type=int,
        default=3,
    )

    return parser.parse_args()


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    args = parse_args()

    run_monitor(
        threshold=args.threshold,
        parallelism=args.parallelism,
    )