"""Executable PyFlink composition root for canonical Streaming Block A."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from data_platform.streaming.config.settings import OperatingMode, RuntimeSettings, load_profile, profile_by_name
from data_platform.streaming.contracts.topics import CONSUMER_GROUPS, TOPICS, consumer_group, topic_name


@dataclass(frozen=True)
class FlinkJobPlan:
    profile: object
    source_topic: str
    consumer_group: str
    historical_topic: str
    online_topic: str
    retry_topic: str
    late_topic: str
    dlq_topic: str
    keyed_operators: tuple[str, ...]


def build_job_plan(profile_name: str) -> FlinkJobPlan:
    profile = profile_by_name(profile_name)
    return FlinkJobPlan(profile, TOPICS["raw"].name, CONSUMER_GROUPS["flink"], TOPICS["accepted"].name, TOPICS["online"].name, TOPICS["retry"].name, TOPICS["late"].name, TOPICS["dlq"].name, ("transaction-dedup", "event-time-disposition", "entity-fan-out", "user-feature-state", "account-feature-state", "device-feature-state", "merchant-feature-state"))


def uses_source_idleness(mode: OperatingMode) -> bool:
    """Benchmark replay drains every partition with markers instead.

    Under backpressure an idle-marked split lets the drain marker advance the
    watermark past records still queued on that split; their timers then never
    fire. Production keeps idleness so a quiet partition cannot stall timers.
    """
    return mode is not OperatingMode.BENCHMARK_REPLAY


class EventTimestampAssigner:
    def extract_timestamp(self, value, record_timestamp) -> int:
        from data_platform.streaming.flink.records import RuntimeEvent
        record = RuntimeEvent.from_json(value)
        if record.event is not None:
            return int(record.event.event_time.timestamp() * 1000)
        if record.control_event_time_ms is not None:
            return record.control_event_time_ms
        raise ValueError("timestamped stream received a record without event time")


@dataclass
class BuiltBlockAJob:
    environment: object
    statement_set: object
    mode: OperatingMode

    def execute(self):
        """Explicit execution boundary; construction never submits a job."""
        return self.statement_set.execute()


def _outbound_row(raw):
    from pyflink.common import Row
    from data_platform.streaming.flink.records import OutboundRecord
    item = OutboundRecord.from_json(raw)
    return Row(item.key, item.payload)


def _add_kafka_sink(table_env, statement_set, stream, name, topic, bootstrap_servers):
    from pyflink.common import Types
    row_stream = stream.map(_outbound_row, output_type=Types.ROW([Types.STRING(), Types.STRING()]))
    view, sink = f"{name}_view", f"{name}_sink"
    table_env.create_temporary_view(view, table_env.from_data_stream(row_stream).alias("message_key", "payload"))
    table_env.execute_sql(f"""CREATE TEMPORARY TABLE {sink} (
      message_key STRING, payload STRING
    ) WITH (
      'connector'='kafka', 'topic'='{topic}',
      'properties.bootstrap.servers'='{bootstrap_servers}',
      'key.format'='raw', 'key.fields'='message_key',
      'value.format'='raw', 'value.fields-include'='EXCEPT_KEY',
      'sink.delivery-guarantee'='at-least-once'
    )""")
    statement_set.add_insert_sql(f"INSERT INTO {sink} SELECT message_key, payload FROM {view}")


def _add_control_log_sink(table_env, statement_set, stream):
    table = table_env.from_data_stream(stream).alias("payload")
    table_env.create_temporary_view("bootstrap_control_view", table)
    table_env.execute_sql("CREATE TEMPORARY TABLE bootstrap_control_log (payload STRING) WITH ('connector'='print', 'print-identifier'='BOOTSTRAP_COMPLETE')")
    statement_set.add_insert_sql("INSERT INTO bootstrap_control_log SELECT payload FROM bootstrap_control_view")


def _parse_operator(settings: RuntimeSettings):
    from data_platform.streaming.flink.operators import ParseAndValidate

    bootstrap_only = settings.mode is OperatingMode.FIRST_DEPLOY_BOOTSTRAP
    return ParseAndValidate(
        settings.dataset_version if bootstrap_only else None,
        settings.source_run_id if bootstrap_only else None,
        settings.cutover_iso if bootstrap_only else None,
        bootstrap_only=bootstrap_only,
        benchmark_replay=settings.mode is OperatingMode.BENCHMARK_REPLAY,
    )


def _state_backend(profile):
    from pyflink.datastream.state_backend import EmbeddedRocksDBStateBackend, HashMapStateBackend

    if profile.state_backend == "rocksdb":
        return EmbeddedRocksDBStateBackend(enable_incremental_checkpointing=profile.incremental_checkpoints)
    return HashMapStateBackend()


def build_executable_job(settings: RuntimeSettings) -> BuiltBlockAJob:
    from pyflink.common import Configuration, Duration, Types, WatermarkStrategy
    from pyflink.common.restart_strategy import RestartStrategies
    from pyflink.common.serialization import SimpleStringSchema
    from pyflink.datastream import CheckpointingMode, ExternalizedCheckpointCleanup, StreamExecutionEnvironment
    from pyflink.datastream.connectors.kafka import KafkaOffsetResetStrategy, KafkaOffsetsInitializer, KafkaSource
    from pyflink.table import StreamTableEnvironment
    from data_platform.streaming.flink.operators import BootstrapCompletionMetric, EntityFanOut, EventTimeDisposition, HistoricalJson, InvalidJson, LateJson, ManagedDeduplicate, ManagedHistoricalFeatures
    from data_platform.streaming.flink.records import EntityEvent, OutboundRecord, RuntimeEvent

    if not settings.kafka_connector_jar.is_file():
        raise FileNotFoundError(f"Kafka connector JAR not found: {settings.kafka_connector_jar}")
    namespace = settings.namespace
    input_parallelism = settings.profile.parallelism
    env = StreamExecutionEnvironment.get_execution_environment()
    runtime_configuration = Configuration()
    if settings.savepoint_path:
        runtime_configuration.set_string("execution.savepoint.path", settings.savepoint_path)
    env.configure(runtime_configuration)
    env.set_parallelism(input_parallelism)
    env.set_state_backend(_state_backend(settings.profile))
    env.enable_checkpointing(int(settings.profile.checkpoint_interval.total_seconds() * 1000), getattr(CheckpointingMode, settings.profile.checkpoint_mode))
    checkpoint = env.get_checkpoint_config()
    checkpoint.set_checkpoint_storage_dir(settings.checkpoint_storage)
    checkpoint.enable_externalized_checkpoints(ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION)
    env.set_restart_strategy(RestartStrategies.fixed_delay_restart(settings.profile.restart_attempts, int(settings.profile.restart_delay.total_seconds() * 1000)))

    # A benchmark namespace is always fresh, so it replays its bounded topic from the start.
    offsets = KafkaOffsetsInitializer.earliest() if settings.mode in {OperatingMode.FIRST_DEPLOY_BOOTSTRAP, OperatingMode.BENCHMARK_REPLAY} else KafkaOffsetsInitializer.committed_offsets(KafkaOffsetResetStrategy.LATEST)
    source_topics = (topic_name("raw", namespace), topic_name("retry", namespace))
    source = (KafkaSource.builder().set_bootstrap_servers(settings.bootstrap_servers).set_topics(*source_topics).set_group_id(consumer_group("flink", namespace)).set_starting_offsets(offsets).set_value_only_deserializer(SimpleStringSchema()).build())
    # The benchmark envelope supplies deterministic logical watermark progress,
    # so correctness can exercise the real profile parallelism (1 versus 4).
    raw = (
        env.from_source(source, WatermarkStrategy.no_watermarks(), "redpanda-raw-and-retry")
        .uid("block-a-redpanda-source-v1")
        .set_parallelism(input_parallelism)
    )
    parsed = raw.map(
        _parse_operator(settings), output_type=Types.STRING()
    ).name("canonical-parse-and-validate").set_parallelism(input_parallelism)
    invalid = parsed.filter(lambda raw: RuntimeEvent.from_json(raw).route == "DLQ").map(InvalidJson(), output_type=Types.STRING())
    control = parsed.filter(lambda raw: RuntimeEvent.from_json(raw).route == "BOOTSTRAP_COMPLETE").map(BootstrapCompletionMetric(), output_type=Types.STRING()).name("bootstrap-completion-gate")
    if settings.mode is OperatingMode.FIRST_DEPLOY_BOOTSTRAP:
        timed_input = parsed.filter(lambda raw: RuntimeEvent.from_json(raw).route == "VALID" and RuntimeEvent.from_json(raw).bootstrap)
    else:
        timed_input = parsed.filter(lambda raw: RuntimeEvent.from_json(raw).route in {"VALID", "WATERMARK"} and not RuntimeEvent.from_json(raw).bootstrap)
    timed_input = timed_input.set_parallelism(input_parallelism)
    watermark = WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_millis(int(settings.profile.event_time.max_out_of_orderness.total_seconds() * 1000)))
    if uses_source_idleness(settings.mode):
        watermark = watermark.with_idleness(Duration.of_millis(int(settings.profile.event_time.idle_timeout.total_seconds() * 1000)))
    watermark = watermark.with_timestamp_assigner(EventTimestampAssigner())
    timestamped = timed_input.assign_timestamps_and_watermarks(watermark).set_parallelism(input_parallelism)
    timed = timestamped.filter(lambda raw: RuntimeEvent.from_json(raw).route == "VALID").set_parallelism(input_parallelism)
    deduped = timed.key_by(lambda raw: RuntimeEvent.from_json(raw).event.transaction_id).process(ManagedDeduplicate(int(settings.profile.dedup_ttl.total_seconds())), output_type=Types.STRING()).name("transaction-dedup").uid("block-a-transaction-dedup-v1").set_parallelism(input_parallelism)
    unique = deduped.filter(lambda raw: RuntimeEvent.from_json(raw).route == "VALID").set_parallelism(input_parallelism)
    disposed = unique.key_by(lambda raw: RuntimeEvent.from_json(raw).event.transaction_id).process(EventTimeDisposition(int(settings.profile.event_time.allowed_lateness.total_seconds() * 1000), benchmark_logical_watermark=settings.mode is OperatingMode.BENCHMARK_REPLAY), output_type=Types.STRING()).name("event-time-disposition").set_parallelism(input_parallelism)
    historical = disposed.filter(lambda raw: RuntimeEvent.from_json(raw).route in {"ELIGIBLE", "TOO_LATE"} and not RuntimeEvent.from_json(raw).bootstrap and RuntimeEvent.from_json(raw).retry_count == 0).map(HistoricalJson(), output_type=Types.STRING())
    late = disposed.filter(lambda raw: RuntimeEvent.from_json(raw).route == "TOO_LATE" and not RuntimeEvent.from_json(raw).bootstrap and RuntimeEvent.from_json(raw).retry_count == 0).map(LateJson(), output_type=Types.STRING())
    eligible = disposed.filter(lambda raw: RuntimeEvent.from_json(raw).route == "ELIGIBLE").set_parallelism(input_parallelism)
    entities = eligible.flat_map(EntityFanOut(), output_type=Types.STRING()).name("entity-fan-out")
    feature_outputs = entities.key_by(lambda raw: EntityEvent.from_json(raw).state_key).process(ManagedHistoricalFeatures(int(settings.profile.event_time.allowed_lateness.total_seconds() * 1000), int(settings.profile.history_ttl.total_seconds() * 1000)), output_type=Types.STRING()).name("managed-31-feature-state").uid("block-a-managed-feature-state-v1")

    table_env = StreamTableEnvironment.create(env)
    statements = table_env.create_statement_set()
    _add_control_log_sink(table_env, statements, control)
    _add_kafka_sink(table_env, statements, historical, "accepted", topic_name("accepted", namespace), settings.bootstrap_servers)
    _add_kafka_sink(table_env, statements, late, "late", topic_name("late", namespace), settings.bootstrap_servers)
    _add_kafka_sink(table_env, statements, invalid, "dlq_invalid", topic_name("dlq", namespace), settings.bootstrap_servers)
    for destination in ("online", "retry", "dlq"):
        stream = feature_outputs.filter(lambda raw, expected=destination: OutboundRecord.from_json(raw).destination == expected)
        _add_kafka_sink(table_env, statements, stream, f"feature_{destination}", topic_name(destination, namespace), settings.bootstrap_servers)
    return BuiltBlockAJob(env, statements, settings.mode)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--mode", required=True, choices=[mode.value for mode in OperatingMode])
    result.add_argument("--profile", choices=("baseline", "optimized"), default="optimized")
    result.add_argument("--profile-config", help="explicit profile YAML; overrides --profile lookup")
    result.add_argument("--namespace", help="isolated topic/group suffix, required by BENCHMARK_REPLAY")
    result.add_argument("--bootstrap-servers", required=True)
    result.add_argument("--checkpoint-storage", required=True)
    result.add_argument("--savepoint-path")
    result.add_argument("--silver-root")
    result.add_argument("--silver-manifest-uri")
    result.add_argument("--source-run-id")
    result.add_argument("--dataset-version")
    result.add_argument("--cutover")
    result.add_argument("--kafka-connector-jar", default=str(Path(__file__).parents[2] / "processing/flink/jars/flink-sql-connector-kafka-3.3.0-1.19.jar"))
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    profile = load_profile(args.profile_config) if args.profile_config else profile_by_name(args.profile)
    print(f"block-a profile={profile.name} parallelism={profile.parallelism} checkpoint_interval={profile.checkpoint_interval} state_backend={profile.state_backend} incremental={profile.incremental_checkpoints} config_sha256={profile.config_sha256}", flush=True)
    settings = RuntimeSettings(OperatingMode(args.mode), profile, args.bootstrap_servers, Path(args.kafka_connector_jar), args.checkpoint_storage, savepoint_path=args.savepoint_path, silver_root=args.silver_root, silver_manifest_uri=args.silver_manifest_uri, source_run_id=args.source_run_id, dataset_version=args.dataset_version, cutover_iso=args.cutover, namespace=args.namespace)
    build_executable_job(settings).execute()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
