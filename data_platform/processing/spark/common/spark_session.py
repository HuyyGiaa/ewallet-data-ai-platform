"""Create a profile-driven SparkSession for local or cloud execution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from data_platform.config import RuntimeConfig, load_runtime_config

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


_DELTA_OPTIONS = {
    "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
    "spark.sql.catalog.spark_catalog": (
        "org.apache.spark.sql.delta.catalog.DeltaCatalog"
    ),
}


def spark_options(
    config: RuntimeConfig,
    *,
    optimized: bool,
) -> dict[str, str]:
    """Build Spark properties without importing or starting PySpark."""

    adaptive_enabled = str(optimized).lower()
    options = dict(_DELTA_OPTIONS)
    options.update(config.spark.configurations)
    options.update(config.storage.options)
    options.update(
        {
            "spark.sql.shuffle.partitions": str(
                config.spark.shuffle_partitions
            ),
            "spark.eventLog.dir": config.spark.event_log_uri,
            "spark.sql.adaptive.enabled": adaptive_enabled,
            "spark.sql.adaptive.skewJoin.enabled": adaptive_enabled,
        }
    )
    return options


def _prepare_local_event_log_directory(event_log_uri: str) -> None:
    parsed = urlsplit(event_log_uri)
    if parsed.scheme != "file":
        return
    if parsed.netloc not in ("", "localhost"):
        raise ValueError(
            "local Spark event log file URI must not contain a remote host"
        )

    event_log_directory = Path(unquote(parsed.path))
    if not event_log_directory.is_absolute():
        raise ValueError("local Spark event log file URI must be absolute")
    event_log_directory.mkdir(parents=True, exist_ok=True)


def _get_or_create_spark(builder: object, config: RuntimeConfig) -> SparkSession:
    """Start Spark without resolving Maven packages when none are configured.

    Dataproc submissions provide the pinned Delta JARs through ``--jars``. Calling
    ``configure_spark_with_delta_pip`` in that environment would add a Maven
    coordinate and make the driver depend on external Ivy resolution before the
    application starts.
    """

    if not config.spark.packages:
        return builder.getOrCreate()

    from delta import configure_spark_with_delta_pip

    return configure_spark_with_delta_pip(
        builder,
        extra_packages=list(config.spark.packages),
    ).getOrCreate()


def create_spark_session(
    app_name: str = "EWallet-Batch-Transformation",
    optimized: bool = True,
    runtime_config: RuntimeConfig | None = None,
) -> SparkSession:
    """Create Spark using one validated runtime environment profile."""

    from pyspark.sql import SparkSession

    config = runtime_config or load_runtime_config()
    _prepare_local_event_log_directory(config.spark.event_log_uri)

    builder = SparkSession.builder.appName(app_name)
    if config.spark.master is not None:
        builder = builder.master(config.spark.master)

    for key, value in spark_options(config, optimized=optimized).items():
        builder = builder.config(key, value)

    spark = _get_or_create_spark(builder, config)
    spark.sparkContext.setLogLevel("WARN")

    print(f"[SPARK] Application : {app_name}")
    print(f"[SPARK] Version     : {spark.version}")
    print(f"[SPARK] Environment : {config.environment}")
    print(f"[SPARK] Storage     : {config.storage.scheme}")
    print(
        "[SPARK] AQE         : "
        f"{spark.conf.get('spark.sql.adaptive.enabled')}"
    )
    print(
        "[SPARK] Skew Join   : "
        f"{spark.conf.get('spark.sql.adaptive.skewJoin.enabled')}"
    )
    print(
        "[SPARK] Shuffle     : "
        f"{spark.conf.get('spark.sql.shuffle.partitions')}"
    )
    return spark
