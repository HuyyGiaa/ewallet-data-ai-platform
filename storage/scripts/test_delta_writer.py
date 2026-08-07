"""
Kiểm thử ghi một bảng nhỏ vào Bronze Layer.

Chạy từ thư mục gốc project:

    python storage/scripts/test_delta_writer.py
"""

from __future__ import annotations

import logging
import sys

from delta_writer import DeltaWriterError, write_delta_table


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
        # merchants chỉ có 300 dòng nên phù hợp để kiểm thử đầu tiên.
        result = write_delta_table(
            table_name="merchants",
            mode="overwrite",
        )

        logger.info("Kiểm thử thành công.")
        logger.info("Table: %s", result.table_name)
        logger.info("URI: %s", result.table_uri)
        logger.info("Rows: %s", f"{result.delta_rows:,}")
        logger.info("Delta version: %d", result.delta_version)

        return 0

    except DeltaWriterError:
        logger.exception("Kiểm thử Delta writer thất bại.")
        return 1

    except Exception:
        logger.exception("Lỗi ngoài dự kiến.")
        return 1


if __name__ == "__main__":
    sys.exit(main())