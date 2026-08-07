from __future__ import annotations

from pyspark.sql import SparkSession
from delta import configure_spark_with_delta_pip


def create_spark_session() -> SparkSession:
    builder = (
        SparkSession.builder
        .appName("EWallet-Batch-Transformation")
        .master("local[*]")

        # ==========================
        # Delta Lake
        # ==========================
        .config(
            "spark.sql.extensions",
            "io.delta.sql.DeltaSparkSessionExtension",
        )
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )

        # ==========================
        # AQE + Skew Join
        # ==========================
        .config(
            "spark.sql.adaptive.enabled",
            "true",
        )
        .config(
            "spark.sql.adaptive.skewJoin.enabled",
            "true",
        )

        # ==========================
        # MinIO / S3A
        # ==========================
        .config(
            "spark.hadoop.fs.s3a.endpoint",
            "http://localhost:9000",
        )
        .config(
            "spark.hadoop.fs.s3a.access.key",
            "minioadmin",
        )
        .config(
            "spark.hadoop.fs.s3a.secret.key",
            "minioadmin",
        )
        .config(
            "spark.hadoop.fs.s3a.path.style.access",
            "true",
        )
        .config(
            "spark.hadoop.fs.s3a.connection.ssl.enabled",
            "false",
        )
        .config(
            "spark.hadoop.fs.s3a.impl",
            "org.apache.hadoop.fs.s3a.S3AFileSystem",
        )
    )

    spark = configure_spark_with_delta_pip(builder).getOrCreate()

    spark.sparkContext.setLogLevel("WARN")

    return spark