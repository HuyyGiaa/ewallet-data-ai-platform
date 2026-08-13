"""
DP4 - Parse transaction stream.

Flow:
    transactions.raw
        -> raw JSON string
        -> parse JSON
        -> structured transaction
        -> print
"""

import json
import logging
from pathlib import Path

from pyflink.common import Types
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import FlinkKafkaConsumer


# Config
BOOTSTRAP_SERVERS = "localhost:9092"
KAFKA_TOPIC = "transactions.raw"
CONSUMER_GROUP = "flink-parsed"

KAFKA_JAR = (
    Path(__file__).resolve().parent
    / "jars"
    / "flink-sql-connector-kafka-3.3.0-1.19.jar"
)


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# Environment
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

# Kafka source
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


# JSON parsing
def parse_transaction(raw_event: str):
    """
    Convert raw Kafka JSON into the fields required
    by the streaming pipeline.
    """

    event = json.loads(raw_event)

    return (
        event["transaction_id"],
        event["user_id"],
        float(event["amount"]),
        event["timestamp"],
    )

# Pipeline
def run_pipeline() -> None:

    logger.info(
        "========== FLINK JSON PARSING =========="
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
            output_type=Types.TUPLE(
                [
                    Types.STRING(),   # transaction_id
                    Types.STRING(),   # user_id
                    Types.DOUBLE(),   # amount
                    Types.STRING(),   # timestamp
                ]
            ),
        )
        .name("parse-transaction-json")
    )

    transaction_stream.print()

    logger.info(
        "Waiting for parsed transaction events..."
    )

    env.execute(
        "EWallet-Flink-JSON-Parsing"
    )

# Entry point
if __name__ == "__main__":
    run_pipeline()