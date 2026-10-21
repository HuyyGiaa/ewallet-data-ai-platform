"""
Khởi tạo các bucket cần thiết cho Data Lakehouse.

Chạy từ thư mục gốc project:

    python -m data_platform.storage.scripts.init_bucket
"""

from __future__ import annotations

import argparse
import logging
import sys

from data_platform.config import load_runtime_config
from data_platform.storage.scripts.config import require_local_runtime
from data_platform.storage.scripts.minio_client import (
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize local MinIO buckets.")
    parser.add_argument(
        "--environment",
        choices=["local", "dev", "gcp"],
        default="local",
        help="Runtime profile; this command only accepts local.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    logger = logging.getLogger(__name__)

    try:
        args = parse_args(argv)
        if args.environment != "local":
            raise RuntimeError(
                "init_bucket is a local-only command; "
                f"selected environment={args.environment!r}"
            )
        config = load_runtime_config(args.environment)
        require_local_runtime(config, "init_bucket")
        client = create_minio_client(config)
        check_minio_connection(client)
        ensure_required_buckets(client, config)

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
