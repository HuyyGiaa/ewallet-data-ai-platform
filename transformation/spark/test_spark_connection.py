from spark_session import create_spark_session


def main():
    spark = None

    try:
        print("[INFO] Khởi tạo SparkSession...")

        spark = create_spark_session()

        print("[INFO] Spark version:", spark.version)

        print("[INFO] AQE:")
        print(
            spark.conf.get(
                "spark.sql.adaptive.enabled"
            )
        )

        print("[INFO] Skew Join:")
        print(
            spark.conf.get(
                "spark.sql.adaptive.skewJoin.enabled"
            )
        )

        bronze_path = "s3a://bronze-zone/merchants"

        print(
            f"[INFO] Đọc Delta table: {bronze_path}"
        )

        df = (
            spark.read
            .format("delta")
            .load(bronze_path)
        )

        print("[INFO] Schema:")
        df.printSchema()

        print("[INFO] 5 rows đầu:")
        df.show(5, truncate=False)

        row_count = df.count()

        print(
            f"[INFO] Tổng số dòng: {row_count}"
        )

        if row_count != 300:
            raise RuntimeError(
                f"Row count merchants không đúng. "
                f"Expected=300, actual={row_count}"
            )

        print(
            "[SUCCESS] Spark đọc Delta từ MinIO thành công."
        )

    except Exception as exc:
        print(
            f"[ERROR] Spark test thất bại: {exc}"
        )
        raise

    finally:
        if spark is not None:
            spark.stop()
            print("[INFO] SparkSession đã dừng.")


if __name__ == "__main__":
    main()