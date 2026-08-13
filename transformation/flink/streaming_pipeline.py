"""
E-Wallet PyFlink Streaming Pipeline

Flow:
    transactions.raw
    -> parse JSON
    -> deduplicate transaction_id
    -> Event Time + Watermark
    -> 5-minute user window
    -> transaction count + total amount

Extra outputs:
    duplicate transactions
    late transactions
"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from pyflink.common import Duration, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.time import Time
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import FlinkKafkaConsumer
from pyflink.datastream.functions import (
    AggregateFunction,
    KeyedProcessFunction,
    ProcessWindowFunction,
)
from pyflink.datastream.state import (
    StateTtlConfig,
    ValueStateDescriptor,
)
from pyflink.datastream.window import TumblingEventTimeWindows


BOOTSTRAP_SERVERS = "localhost:9092"
KAFKA_TOPIC = "transactions.raw"
CONSUMER_GROUP = "flink-streaming-pipeline"

WINDOW_MINUTES = 5
WATERMARK_DELAY_SECONDS = 10
ALLOWED_LATENESS_SECONDS = 60
DEDUP_TTL_MINUTES = 30
IDLE_TIMEOUT_SECONDS = 30

KAFKA_JAR = (
    Path(__file__).resolve().parent
    / "jars"
    / "flink-sql-connector-kafka-3.3.0-1.19.jar"
)


# transaction_id, user_id, amount, timestamp
TRANSACTION_TYPE = Types.TUPLE([
    Types.STRING(),
    Types.STRING(),
    Types.DOUBLE(),
    Types.STRING(),
])

# is_duplicate, transaction_id, user_id, amount, timestamp
DEDUP_RESULT_TYPE = Types.TUPLE([
    Types.BOOLEAN(),
    Types.STRING(),
    Types.STRING(),
    Types.DOUBLE(),
    Types.STRING(),
])

# is_late, transaction_id, user_id, amount, timestamp
LATE_RESULT_TYPE = Types.TUPLE([
    Types.BOOLEAN(),
    Types.STRING(),
    Types.STRING(),
    Types.DOUBLE(),
    Types.STRING(),
])

# user_id, window_start, window_end, count, total_amount
FEATURE_TYPE = Types.TUPLE([
    Types.STRING(),
    Types.LONG(),
    Types.LONG(),
    Types.LONG(),
    Types.DOUBLE(),
])


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


def create_environment(
    parallelism: int,
) -> StreamExecutionEnvironment:

    if not KAFKA_JAR.exists():
        raise FileNotFoundError(
            f"Kafka connector JAR not found: {KAFKA_JAR}"
        )

    env = StreamExecutionEnvironment.get_execution_environment()

    env.set_parallelism(parallelism)
    env.add_jars(KAFKA_JAR.as_uri())
    env.get_config().set_auto_watermark_interval(1000)

    return env


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


def parse_transaction(raw_event: str):

    event = json.loads(raw_event)

    return (
        event["transaction_id"],
        event["user_id"],
        float(event["amount"]),
        event["timestamp"],
    )


class DeduplicateTransactions(KeyedProcessFunction):
    """
    Mark repeated transaction_id values using keyed state.
    """

    def open(self, runtime_context):

        state_descriptor = ValueStateDescriptor(
            "transaction-seen",
            Types.BOOLEAN(),
        )

        ttl_config = (
            StateTtlConfig
            .new_builder(
                Time.minutes(DEDUP_TTL_MINUTES)
            )
            .update_ttl_on_create_and_write()
            .never_return_expired()
            .build()
        )

        state_descriptor.enable_time_to_live(
            ttl_config
        )

        self.seen_state = runtime_context.get_state(
            state_descriptor
        )

    def process_element(self, value, ctx):

        is_duplicate = bool(
            self.seen_state.value()
        )

        if not is_duplicate:
            self.seen_state.update(True)

        yield (
            is_duplicate,
            value[0],
            value[1],
            value[2],
            value[3],
        )


def to_epoch_millis(
    timestamp_text: str,
) -> int:

    event_time = datetime.fromisoformat(
        timestamp_text
    )

    return int(
        event_time.timestamp() * 1000
    )


class TransactionTimestampAssigner(TimestampAssigner):

    def extract_timestamp(
        self,
        value,
        record_timestamp,
    ) -> int:

        return to_epoch_millis(
            value[3]
        )


def create_watermark_strategy() -> WatermarkStrategy:

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
        .with_idleness(
            Duration.of_seconds(
                IDLE_TIMEOUT_SECONDS
            )
        )
    )


class ClassifyLateTransaction(KeyedProcessFunction):
    """
    Mark events that are already beyond:
        window end + allowed lateness.

    This branch is used for evidence.
    The main event-time window still applies the real
    allowed-lateness policy.
    """

    def process_element(self, value, ctx):

        event_time = to_epoch_millis(
            value[3]
        )

        watermark = (
            ctx
            .timer_service()
            .current_watermark()
        )

        window_ms = (
            WINDOW_MINUTES
            * 60
            * 1000
        )

        window_end = (
            (event_time // window_ms) + 1
        ) * window_ms

        cleanup_time = (
            window_end
            - 1
            + ALLOWED_LATENESS_SECONDS * 1000
        )

        is_late = (
            watermark >= cleanup_time
        )

        yield (
            is_late,
            value[0],
            value[1],
            value[2],
            value[3],
        )


class TransactionAggregate(AggregateFunction):
    """
    Incrementally maintain:
        transaction_count
        total_amount
    """

    def create_accumulator(self):
        return 0, 0.0

    def add(self, value, accumulator):

        count, total_amount = accumulator

        return (
            count + 1,
            total_amount + value[2],
        )

    def get_result(self, accumulator):
        return accumulator

    def merge(self, first, second):

        return (
            first[0] + second[0],
            first[1] + second[1],
        )


class UserWindowResult(ProcessWindowFunction):

    def process(
        self,
        key,
        context,
        aggregates,
    ):

        count, total_amount = next(
            iter(aggregates)
        )

        window = context.window()

        yield (
            key,
            window.start,
            window.end,
            count,
            total_amount,
        )


def format_time(epoch_ms: int) -> str:

    return datetime.fromtimestamp(
        epoch_ms / 1000
    ).isoformat()


def format_feature(value) -> str:

    user_id, start, end, count, amount = value

    return (
        f"user_id={user_id} | "
        f"window={format_time(start)} -> {format_time(end)} | "
        f"count={count} | "
        f"amount={amount:.2f}"
    )


def format_transaction(
    label: str,
    value,
) -> str:

    return (
        f"{label} | "
        f"transaction_id={value[0]} | "
        f"user_id={value[1]} | "
        f"amount={value[2]:.2f} | "
        f"event_time={value[3]}"
    )


def run_pipeline(
    parallelism: int,
) -> None:

    logger.info("Starting E-Wallet Flink pipeline")

    logger.info(
        "topic=%s | parallelism=%s | window=%sm | "
        "watermark=%ss | lateness=%ss | dedup_ttl=%sm",
        KAFKA_TOPIC,
        parallelism,
        WINDOW_MINUTES,
        WATERMARK_DELAY_SECONDS,
        ALLOWED_LATENESS_SECONDS,
        DEDUP_TTL_MINUTES,
    )

    env = create_environment(
        parallelism
    )

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

    # Each transaction_id owns one dedup state.
    dedup_result_stream = (
        transaction_stream
        .key_by(
            lambda tx: tx[0],
            key_type=Types.STRING(),
        )
        .process(
            DeduplicateTransactions(),
            output_type=DEDUP_RESULT_TYPE,
        )
        .name("deduplicate-transactions")
    )

    deduplicated_stream = (
        dedup_result_stream
        .filter(
            lambda tx: not tx[0]
        )
        .map(
            lambda tx: tx[1:],
            output_type=TRANSACTION_TYPE,
        )
        .name("unique-transactions")
    )

    duplicate_stream = (
        dedup_result_stream
        .filter(
            lambda tx: tx[0]
        )
        .map(
            lambda tx: tx[1:],
            output_type=TRANSACTION_TYPE,
        )
        .name("duplicate-transactions")
    )

    timed_stream = (
        deduplicated_stream
        .assign_timestamps_and_watermarks(
            create_watermark_strategy()
        )
        .name("assign-event-time-watermarks")
    )

    # Separate branch only records events that are too late.
    late_result_stream = (
        timed_stream
        .key_by(
            lambda tx: tx[1],
            key_type=Types.STRING(),
        )
        .process(
            ClassifyLateTransaction(),
            output_type=LATE_RESULT_TYPE,
        )
        .name("classify-late-transactions")
    )

    late_stream = (
        late_result_stream
        .filter(
            lambda tx: tx[0]
        )
        .map(
            lambda tx: tx[1:],
            output_type=TRANSACTION_TYPE,
        )
        .name("late-transactions")
    )

    # Main branch stays directly on timed_stream so
    # Event Time metadata is preserved for the window.
    windowed_stream = (
        timed_stream
        .key_by(
            lambda tx: tx[1],
            key_type=Types.STRING(),
        )
        .window(
            TumblingEventTimeWindows.of(
                Time.minutes(
                    WINDOW_MINUTES
                )
            )
        )
        .allowed_lateness(
            ALLOWED_LATENESS_SECONDS
            * 1000
        )
    )

    feature_stream = (
        windowed_stream
        .aggregate(
            TransactionAggregate(),
            window_function=UserWindowResult(),
            accumulator_type=Types.TUPLE([
                Types.LONG(),
                Types.DOUBLE(),
            ]),
            output_type=FEATURE_TYPE,
        )
        .name("user-5m-features")
    )

    (
        feature_stream
        .map(
            format_feature,
            output_type=Types.STRING(),
        )
        .print("FEATURE")
    )

    (
        duplicate_stream
        .map(
            lambda tx: format_transaction(
                "DUPLICATE",
                tx,
            ),
            output_type=Types.STRING(),
        )
        .print("DUPLICATE")
    )

    (
        late_stream
        .map(
            lambda tx: format_transaction(
                "LATE",
                tx,
            ),
            output_type=Types.STRING(),
        )
        .print("LATE")
    )

    env.execute(
        "EWallet-Flink-Streaming-Pipeline"
    )


def parse_args():

    parser = argparse.ArgumentParser(
        description="E-Wallet PyFlink streaming pipeline"
    )

    parser.add_argument(
        "--parallelism",
        type=int,
        default=3,
        help="Flink parallelism (default: 3)",
    )

    args = parser.parse_args()

    if args.parallelism < 1:
        parser.error(
            "--parallelism must be >= 1"
        )

    return args


if __name__ == "__main__":

    args = parse_args()

    run_pipeline(
        parallelism=args.parallelism
    )