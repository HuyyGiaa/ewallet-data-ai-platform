"""
DP4 - Flink Streaming Baseline

Flow:
    Redpanda transactions.raw
        -> PyFlink
        -> raw string
        -> print sink

This baseline only verifies that Flink can consume
transaction events from Redpanda.
"""

from pathlib import Path
import logging

from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import FlinkKafkaConsumer

# Config
BOOTSTRAP_SERVERS = "localhost:9092"
KAFKA_TOPIC = "transactions.raw"
CONSUMER_GROUP = "flink-baseline"

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

# Flink environment
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

    env.add_jars(
        KAFKA_JAR.as_uri()
    )

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

    # Baseline only consumes events produced after the job starts.
    consumer.set_start_from_latest()

    return consumer

# Pipeline
def run_baseline() -> None:

    logger.info("========== FLINK STREAMING BASELINE ==========")
    logger.info("Bootstrap servers: %s", BOOTSTRAP_SERVERS)
    logger.info("Topic: %s", KAFKA_TOPIC)
    logger.info("Consumer group: %s", CONSUMER_GROUP)

    env = create_environment()

    consumer = create_kafka_consumer()

    transaction_stream = (
        env
        .add_source(consumer)
        .name("transactions-raw-source")
    )

    transaction_stream.print()

    logger.info(
        "Waiting for transaction events..."
    )

    env.execute(
        "EWallet-Flink-Streaming-Baseline"
    )

# Entry point
if __name__ == "__main__":
    run_baseline()