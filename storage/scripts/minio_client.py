"""
Các hàm quản lý bucket MinIO cho Storage Layer.
"""

from __future__ import annotations

import logging

from minio import Minio
from minio.error import S3Error

from config import (
    MINIO_ACCESS_KEY,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
    REQUIRED_BUCKETS,
)


logger = logging.getLogger(__name__)


class MinioStorageError(RuntimeError):
    """Lỗi kết nối hoặc thao tác với MinIO."""


def create_minio_client() -> Minio:
    logger.info("Khởi tạo MinIO client tại %s", MINIO_ENDPOINT)

    return Minio(
        endpoint=MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def check_minio_connection(client: Minio) -> None:
    try:
        buckets = client.list_buckets()
        logger.info(
            "Kết nối MinIO thành công. Hiện có %d bucket.",
            len(buckets),
        )
    except S3Error as exc:
        raise MinioStorageError(
            f"MinIO trả về lỗi S3: {exc.code} - {exc.message}"
        ) from exc
    except Exception as exc:
        raise MinioStorageError(
            f"Không thể kết nối MinIO tại {MINIO_ENDPOINT}: {exc}"
        ) from exc


def ensure_bucket(client: Minio, bucket_name: str) -> bool:
    try:
        if client.bucket_exists(bucket_name):
            logger.info("Bucket đã tồn tại: %s", bucket_name)
            return False

        client.make_bucket(bucket_name)
        logger.info("Đã tạo bucket: %s", bucket_name)
        return True

    except S3Error as exc:
        raise MinioStorageError(
            f"Không thể kiểm tra hoặc tạo bucket '{bucket_name}': "
            f"{exc.code} - {exc.message}"
        ) from exc
    except Exception as exc:
        raise MinioStorageError(
            f"Lỗi không xác định khi xử lý bucket '{bucket_name}': {exc}"
        ) from exc


def ensure_required_buckets(client: Minio) -> None:
    """Kiểm tra và tạo toàn bộ bucket cần thiết cho Lakehouse."""
    created_count = 0

    for bucket_name in REQUIRED_BUCKETS:
        if ensure_bucket(client, bucket_name):
            created_count += 1

    logger.info(
        "Hoàn tất kiểm tra bucket: %d bucket mới, %d bucket đã tồn tại.",
        created_count,
        len(REQUIRED_BUCKETS) - created_count,
    )