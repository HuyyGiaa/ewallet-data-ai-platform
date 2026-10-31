"""Fail-closed runtime checks for GCP Spark jobs that read Delta on GCS."""

from __future__ import annotations

from data_platform.config import RuntimeConfig


def validate_gcp_spark_runtime(
    spark: object,
    runtime_config: RuntimeConfig,
    *,
    output_uri: str,
) -> None:
    if runtime_config.environment != "gcp" or runtime_config.storage.scheme != "gs":
        return
    if not output_uri.startswith("gs://"):
        raise ValueError("GCP Spark output must use a gs:// URI")

    master = str(spark.sparkContext.master)
    if master.startswith("local"):
        raise RuntimeError("GCP export refuses a local Spark master")

    expected = {
        "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog": (
            "org.apache.spark.sql.delta.catalog.DeltaCatalog"
        ),
    }
    for key, value in expected.items():
        if spark.conf.get(key) != value:
            raise RuntimeError(f"GCP Spark runtime is missing required {key}")

    hadoop_path = spark._jvm.org.apache.hadoop.fs.Path(output_uri)
    filesystem = hadoop_path.getFileSystem(spark._jsc.hadoopConfiguration())
    if str(filesystem.getUri().getScheme()) != "gs":
        raise RuntimeError("GCP Spark runtime is missing the Cloud Storage connector")
