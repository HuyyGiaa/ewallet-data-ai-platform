"""
Kiểm thử đăng ký bảng merchants vào Trino.

Điều kiện:
- merchants đã được ghi thành Delta Lake.
- Trino và MinIO đang chạy.
- register_table procedure đã được bật.

Chạy:

    python storage/scripts/test_trino_client.py
"""

from __future__ import annotations

import logging
import sys

from trino_client import (
    TrinoClientError,
    check_trino_connection,
    register_and_verify_table,
    trino_cursor,
)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)

    try:
        with trino_cursor() as cursor:
            check_trino_connection(cursor)

            result = register_and_verify_table(
                cursor=cursor,
                table_name="merchants",
                expected_rows=300,
            )

        logger.info("Kiểm thử Trino thành công.")
        logger.info(
            "Table: %s.%s.%s",
            result.catalog,
            result.schema,
            result.table,
        )
        logger.info("Location: %s", result.location)
        logger.info("Rows: %s", f"{result.row_count:,}")
        logger.info(
            "Registered now: %s",
            result.registered_now,
        )

        return 0

    except TrinoClientError:
        logger.exception("Kiểm thử Trino client thất bại.")
        return 1

    except Exception:
        logger.exception("Lỗi ngoài dự kiến.")
        return 1


if __name__ == "__main__":
    sys.exit(main())