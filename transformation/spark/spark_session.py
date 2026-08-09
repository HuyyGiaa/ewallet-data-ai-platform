"""
Khởi tạo SparkSession cho Batch Transformation Layer.

Hỗ trợ 2 chế độ:

1. Baseline:
   - AQE OFF
   - Skew Join optimization OFF

2. Optimized:
   - AQE ON
   - Skew Join optimization ON

Spark chạy local[*] và đọc/ghi Delta Lake trên MinIO qua S3A.
"""

from __future__ import annotations

from pyspark.sql import SparkSession
from delta import configure_spark_with_delta_pip
from pathlib import Path

SPARK_EVENT_LOG_DIR = Path("/tmp/spark-events")
SPARK_EVENT_LOG_DIR.mkdir(parents=True, exist_ok=True)

MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"


def create_spark_session(
    app_name: str = "EWallet-Batch-Transformation",
    optimized: bool = True,
) -> SparkSession:
    """
    Tạo SparkSession dùng cho batch processing.

    Args:
        app_name:
            Tên Spark application.

        optimized:
            True:
                bật AQE + skew join optimization.

            False:
                tắt AQE để chạy baseline benchmark.

    Returns:
        SparkSession.
    """

    adaptive_enabled = str(optimized).lower()

    builder = (
        SparkSession.builder
        .appName(app_name)
        .master("local[*]")

        # Delta Lake
        .config(
            "spark.sql.extensions",
            "io.delta.sql.DeltaSparkSessionExtension",
        )
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )

        # Adaptive Query Execution
        .config(
            "spark.sql.adaptive.enabled",
            adaptive_enabled,
        )
        .config(
            "spark.sql.adaptive.skewJoin.enabled",
            adaptive_enabled,
        )

        # MinIO / S3A
        .config(
            "spark.hadoop.fs.s3a.endpoint",
            MINIO_ENDPOINT,
        )
        .config(
            "spark.hadoop.fs.s3a.access.key",
            MINIO_ACCESS_KEY,
        )
        .config(
            "spark.hadoop.fs.s3a.secret.key",
            MINIO_SECRET_KEY,
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
        .config(
            "spark.jars.packages",
            "org.apache.hadoop:hadoop-aws:3.4.2",
        )

        # Shuffle
        #
        # Dataset coursework chưa quá lớn.
        # 32 partitions đủ để demo local mà không tạo quá nhiều
        # small tasks.
        .config(
            "spark.sql.shuffle.partitions",
            "32",
        )
        .config(
            "spark.eventLog.enabled",
            "true",
        )
        .config(
            "spark.eventLog.dir",
            f"file://{SPARK_EVENT_LOG_DIR}",
        )
        .config(
            "spark.ui.enabled",
            "true",
        )
    )

    spark = configure_spark_with_delta_pip(
        builder,
        extra_packages=[
            "org.apache.hadoop:hadoop-aws:3.4.2"
        ],
    ).getOrCreate()

    spark.sparkContext.setLogLevel("WARN")

    print(
        f"[SPARK] Application : {app_name}"
    )
    print(
        f"[SPARK] Version     : {spark.version}"
    )
    print(
        f"[SPARK] AQE         : "
        f"{spark.conf.get('spark.sql.adaptive.enabled')}"
    )
    print(
        f"[SPARK] Skew Join   : "
        f"{spark.conf.get('spark.sql.adaptive.skewJoin.enabled')}"
    )

    return spark