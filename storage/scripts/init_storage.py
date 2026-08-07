"""
Bootstrap Storage Layer cho Data Lakehouse.

Quy trình:
1. Kiểm tra kết nối MinIO.
2. Đảm bảo các bucket bắt buộc tồn tại.
3. Kiểm tra kết nối Trino.
4. Tạo schema delta.bronze_zone nếu chưa tồn tại.
5. Đọc từng file Parquet offline.
6. Ghi thành Delta Lake vào bronze-zone.
7. Đăng ký Delta table vào Trino.
8. Đối chiếu số dòng giữa Delta Lake và Trino.

Chạy toàn bộ:

    python storage/scripts/init_storage.py

Chạy một số bảng:

    python storage/scripts/init_storage.py --tables merchants users accounts

Mặc định sử dụng mode=overwrite vì đây là bước bootstrap dữ liệu offline.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

from config import (
    BRONZE_BUCKET,
    BRONZE_SCHEMA,
    OFFLINE_TABLES,
    TRINO_CATALOG,
)
from delta_writer import (
    DeltaWriteResult,
    DeltaWriterError,
    write_delta_table,
)
from minio_client import (
    MinioStorageError,
    check_minio_connection,
    create_minio_client,
    ensure_required_buckets,
)
from trino_client import (
    TrinoClientError,
    TrinoTableResult,
    check_trino_connection,
    ensure_schema,
    register_and_verify_table,
    trino_cursor,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageTableResult:
    """Kết quả bootstrap hoàn chỉnh của một bảng."""

    table_name: str
    source_rows: int
    delta_rows: int
    trino_rows: int
    delta_version: int
    delta_uri: str
    registered_now: bool
    elapsed_seconds: float


class StorageInitializationError(RuntimeError):
    """Lỗi tổng quát trong quá trình bootstrap Storage Layer."""


def configure_logging(verbose: bool = False) -> None:
    """Cấu hình logging dùng chung cho script."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=(
            "%(asctime)s | %(levelname)-8s | "
            "%(name)s | %(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Giảm log HTTP không cần thiết từ thư viện bên thứ ba.
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("trino").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ghi dữ liệu offline thành Delta Lake và đăng ký "
            "các bảng Bronze vào Trino."
        )
    )

    parser.add_argument(
        "--tables",
        nargs="+",
        default=None,
        help=(
            "Danh sách bảng cần bootstrap. "
            "Mặc định chạy toàn bộ 7 bảng offline."
        ),
    )

    parser.add_argument(
        "--mode",
        choices=("overwrite", "append"),
        default="overwrite",
        help=(
            "Chế độ ghi Delta Lake. "
            "Mặc định: overwrite."
        ),
    )

    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help=(
            "Tiếp tục xử lý các bảng còn lại khi một bảng thất bại. "
            "Mặc định script dừng ngay."
        ),
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Bật DEBUG logging.",
    )

    return parser.parse_args()


def validate_selected_tables(
    selected_tables: list[str] | None,
) -> list[str]:
    """
    Kiểm tra danh sách bảng người dùng truyền vào.

    Returns:
        Danh sách bảng theo đúng thứ tự cần xử lý.
    """
    if selected_tables is None:
        return list(OFFLINE_TABLES)

    invalid_tables = sorted(
        set(selected_tables).difference(OFFLINE_TABLES)
    )

    if invalid_tables:
        raise StorageInitializationError(
            "Các bảng không hợp lệ: "
            f"{', '.join(invalid_tables)}. "
            "Danh sách được hỗ trợ: "
            f"{', '.join(OFFLINE_TABLES)}"
        )

    # Loại trùng nhưng giữ nguyên thứ tự nhập.
    return list(dict.fromkeys(selected_tables))


def initialize_infrastructure() -> None:
    """Kiểm tra MinIO và các bucket bắt buộc."""
    logger.info("========== KIỂM TRA HẠ TẦNG MINIO ==========")

    minio_client = create_minio_client()
    check_minio_connection(minio_client)
    ensure_required_buckets(minio_client)


def bootstrap_table(
    cursor,
    table_name: str,
    mode: str,
) -> StorageTableResult:
    """
    Ghi một bảng vào Delta Lake, đăng ký và xác minh qua Trino.
    """
    started_at = time.perf_counter()

    logger.info("Bắt đầu bootstrap bảng '%s'.", table_name)

    delta_result: DeltaWriteResult = write_delta_table(
        table_name=table_name,
        bucket=BRONZE_BUCKET,
        mode=mode,
    )

    trino_result = register_and_verify_table(
        cursor=cursor,
        table_name=table_name,
        expected_rows=delta_result.delta_rows,
        schema_name=BRONZE_SCHEMA,
        bucket_name=BRONZE_BUCKET,
        ensure_schema_exists=False,
    )

    elapsed_seconds = time.perf_counter() - started_at

    result = StorageTableResult(
        table_name=table_name,
        source_rows=delta_result.source_rows,
        delta_rows=delta_result.delta_rows,
        trino_rows=trino_result.row_count,
        delta_version=delta_result.delta_version,
        delta_uri=delta_result.table_uri,
        registered_now=trino_result.registered_now,
        elapsed_seconds=elapsed_seconds,
    )

    logger.info(
        "[%s] Bootstrap thành công trong %.2f giây.",
        table_name,
        elapsed_seconds,
    )

    return result


def log_summary(
    results: list[StorageTableResult],
    failed_tables: list[tuple[str, str]],
) -> None:
    """In báo cáo tổng kết."""
    logger.info("")
    logger.info("============== STORAGE SUMMARY ==============")

    if results:
        header = (
            f"{'TABLE':<24}"
            f"{'SOURCE':>12}"
            f"{'DELTA':>12}"
            f"{'TRINO':>12}"
            f"{'VERSION':>10}"
            f"{'TIME(S)':>12}"
        )

        logger.info(header)
        logger.info("-" * len(header))

        for result in results:
            logger.info(
                "%-24s%12s%12s%12s%10d%12.2f",
                result.table_name,
                f"{result.source_rows:,}",
                f"{result.delta_rows:,}",
                f"{result.trino_rows:,}",
                result.delta_version,
                result.elapsed_seconds,
            )

    if failed_tables:
        logger.error("")
        logger.error("Các bảng thất bại:")

        for table_name, error_message in failed_tables:
            logger.error(
                "- %s: %s",
                table_name,
                error_message,
            )

    logger.info("")
    logger.info(
        "Thành công: %d | Thất bại: %d",
        len(results),
        len(failed_tables),
    )
    logger.info("==============================================")


def run(
    tables: list[str],
    mode: str,
    continue_on_error: bool,
) -> int:
    """Điều phối toàn bộ quá trình bootstrap."""
    results: list[StorageTableResult] = []
    failed_tables: list[tuple[str, str]] = []

    logger.info("Bắt đầu bootstrap Storage Layer.")
    logger.info("Bucket đích: %s", BRONZE_BUCKET)
    logger.info(
        "Trino schema: %s.%s",
        TRINO_CATALOG,
        BRONZE_SCHEMA,
    )
    logger.info("Write mode: %s", mode)
    logger.info("Số bảng: %d", len(tables))
    logger.info("Danh sách: %s", ", ".join(tables))

    initialize_infrastructure()

    logger.info("")
    logger.info("========== KIỂM TRA TRINO ==========")

    with trino_cursor() as cursor:
        check_trino_connection(cursor)

        # Chỉ cần tạo schema một lần trước vòng lặp.
        ensure_schema(
            cursor=cursor,
            schema_name=BRONZE_SCHEMA,
            bucket_name=BRONZE_BUCKET,
        )

        for index, table_name in enumerate(tables, start=1):
            logger.info("")
            logger.info(
                "========== [%d/%d] %s ==========",
                index,
                len(tables),
                table_name,
            )

            try:
                result = bootstrap_table(
                    cursor=cursor,
                    table_name=table_name,
                    mode=mode,
                )
                results.append(result)

            except (
                DeltaWriterError,
                TrinoClientError,
                Exception,
            ) as exc:
                logger.exception(
                    "[%s] Bootstrap thất bại.",
                    table_name,
                )

                failed_tables.append(
                    (table_name, str(exc))
                )

                if not continue_on_error:
                    logger.error(
                        "Dừng pipeline vì "
                        "--continue-on-error không được bật."
                    )
                    break

    log_summary(results, failed_tables)

    if failed_tables:
        return 1

    logger.info(
        "Storage Layer đã bootstrap thành công toàn bộ."
    )
    return 0


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)

    try:
        tables = validate_selected_tables(args.tables)

        return run(
            tables=tables,
            mode=args.mode,
            continue_on_error=args.continue_on_error,
        )

    except (
        StorageInitializationError,
        MinioStorageError,
        DeltaWriterError,
        TrinoClientError,
    ):
        logger.exception("Khởi tạo Storage Layer thất bại.")
        return 1

    except KeyboardInterrupt:
        logger.warning("Người dùng đã dừng chương trình.")
        return 130

    except Exception:
        logger.exception("Xảy ra lỗi ngoài dự kiến.")
        return 1


if __name__ == "__main__":
    sys.exit(main())