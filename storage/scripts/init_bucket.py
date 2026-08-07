"""
Khởi tạo các bucket cần thiết cho Data Lakehouse.

Chạy từ thư mục gốc project:

    python storage/scripts/init_bucket.py
"""

from __future__ import annotations

import logging
import sys

from minio_client import (
    MinioStorageError,
    check_minio_connection,
    create_minio_client,
    ensure_required_buckets,
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
        client = create_minio_client()
        check_minio_connection(client)
        ensure_required_buckets(client)

        logger.info("Khởi tạo MinIO Storage hoàn tất.")
        return 0

    except MinioStorageError:
        logger.exception("Khởi tạo MinIO Storage thất bại.")
        return 1

    except Exception:
        logger.exception("Xảy ra lỗi ngoài dự kiến.")
        return 1


if __name__ == "__main__":
    sys.exit(main())