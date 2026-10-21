"""
Các hàm quản lý bucket MinIO cho Storage Layer.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error

from data_platform.config import RuntimeConfig
from data_platform.storage.scripts.config import (
    local_bucket_names,
    require_local_runtime,
)


logger = logging.getLogger(__name__)


class MinioStorageError(RuntimeError):
    """Lỗi kết nối hoặc thao tác với MinIO."""


def create_minio_client(runtime_config: RuntimeConfig) -> Minio:
    require_local_runtime(runtime_config, "MinIO client")
    options = runtime_config.storage.options
    endpoint_url = options.get("spark.hadoop.fs.s3a.endpoint")
    access_key = options.get("spark.hadoop.fs.s3a.access.key")
    secret_key = options.get("spark.hadoop.fs.s3a.secret.key")
    if not endpoint_url or not access_key or not secret_key:
        raise MinioStorageError(
            "Local runtime profile must configure MinIO endpoint and credentials."
        )
    parsed = urlsplit(endpoint_url)
    endpoint = parsed.netloc or parsed.path
    secure = parsed.scheme == "https"
    logger.info("Khởi tạo MinIO client tại %s", endpoint)

    return Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
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
            f"Không thể kết nối MinIO: {exc}"
        ) from exc


def delta_log_exists(
    client: Minio,
    bucket_name: str,
    table_name: str,
) -> bool:
    """Kiểm tra một table path có chứa Delta transaction log hay không."""
    prefix = f"{table_name}/_delta_log/"

    try:
        return next(
            iter(
                client.list_objects(
                    bucket_name,
                    prefix=prefix,
                    recursive=True,
                )
            ),
            None,
        ) is not None
    except S3Error as exc:
        raise MinioStorageError(
            f"Không thể kiểm tra Delta log tại "
            f"s3://{bucket_name}/{table_name}: "
            f"{exc.code} - {exc.message}"
        ) from exc
    except Exception as exc:
        raise MinioStorageError(
            f"Không thể kiểm tra Delta log tại "
            f"s3://{bucket_name}/{table_name}: {exc}"
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


def ensure_required_buckets(
    client: Minio,
    runtime_config: RuntimeConfig,
) -> None:
    """Kiểm tra và tạo toàn bộ bucket cần thiết cho Lakehouse."""
    created_count = 0
    required_buckets = local_bucket_names(runtime_config.storage)

    for bucket_name in required_buckets:
        if ensure_bucket(client, bucket_name):
            created_count += 1

    logger.info(
        "Hoàn tất kiểm tra bucket: %d bucket mới, %d bucket đã tồn tại.",
        created_count,
        len(required_buckets) - created_count,
    )
