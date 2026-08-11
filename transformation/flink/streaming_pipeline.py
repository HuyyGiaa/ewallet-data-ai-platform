"""
DP4 - E-Wallet Streaming Pipeline

Current flow:
    Redpanda
        -> parse JSON
        -> assign Event Time
        -> generate Watermarks
        -> print

Later stages will add:
    -> keyBy
    -> 5-minute window
    -> late-event handling
    -> duplicate handling
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from pyflink.common import Duration, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import FlinkKafkaConsumer


# ============================================================
# Config
# ============================================================

BOOTSTRAP_SERVERS = "localhost:9092"
KAFKA_TOPIC = "transactions.raw"
CONSUMER_GROUP = "flink-streaming-pipeline"

WATERMARK_DELAY_SECONDS = 10

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
# Environment
# ============================================================

def create_environment() -> StreamExecutionEnvironment:

    if not KAFKA_JAR.exists():
        raise FileNotFoundError(
            f"Kafka connector JAR not found: {KAFKA_JAR}"
        )

    env = (
        StreamExecutionEnvironment
        .get_execution_environment()
    )

    env.set_parallelism(1)
    env.add_jars(KAFKA_JAR.as_uri())

    return env


# ============================================================
# Kafka source
# ============================================================

def create_kafka_consumer() -> FlinkKafkaConsumer:

    consumer = FlinkKafkaConsumer(
        topics=KAFKA_TOPIC,
        deserialization_schema=SimpleStringSchema(),
        properties={
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": CONSUMER_GROUP,
        },
    )

    consumer.set_start_from_latest()

    return consumer


# ============================================================
# Parsing
# ============================================================

def parse_transaction(raw_event: str):

    event = json.loads(raw_event)

    return (
        event["transaction_id"],
        event["user_id"],
        float(event["amount"]),
        event["timestamp"],
    )


TRANSACTION_TYPE = Types.TUPLE(
    [
        Types.STRING(),   # transaction_id
        Types.STRING(),   # user_id
        Types.DOUBLE(),   # amount
        Types.STRING(),   # timestamp
    ]
)


# ============================================================
# Event Time
# ============================================================

class TransactionTimestampAssigner(TimestampAssigner):
    """
    Extract event time from the transaction timestamp.

    Flink expects timestamps in milliseconds since Epoch.
    """

    def extract_timestamp(
        self,
        value,
        record_timestamp,
    ) -> int:

        event_time = datetime.fromisoformat(
            value[3]
        )

        return int(
            event_time.timestamp() * 1000
        )


def create_watermark_strategy() -> WatermarkStrategy:
    """
    Allow events to arrive up to 10 seconds out of order.
    """

    return (
        WatermarkStrategy
        .for_bounded_out_of_orderness(
            Duration.of_seconds(
                WATERMARK_DELAY_SECONDS
            )
        )
        .with_timestamp_assigner(
            TransactionTimestampAssigner()
        )
    )


# ============================================================
# Pipeline
# ============================================================

def run_pipeline() -> None:

    logger.info(
        "========== FLINK STREAMING PIPELINE =========="
    )

    logger.info(
        "Topic: %s",
        KAFKA_TOPIC,
    )

    logger.info(
        "Watermark delay: %s seconds",
        WATERMARK_DELAY_SECONDS,
    )

    env = create_environment()

    raw_stream = (
        env
        .add_source(
            create_kafka_consumer()
        )
        .name("transactions-raw-source")
    )

    transaction_stream = (
        raw_stream
        .map(
            parse_transaction,
            output_type=TRANSACTION_TYPE,
        )
        .name("parse-transaction-json")
    )

    timed_stream = (
        transaction_stream
        .assign_timestamps_and_watermarks(
            create_watermark_strategy()
        )
        .name("assign-event-time-watermarks")
    )

    timed_stream.print()

    logger.info(
        "Waiting for transaction events..."
    )

    env.execute(
        "EWallet-Flink-Streaming-Pipeline"
    )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    run_pipeline()