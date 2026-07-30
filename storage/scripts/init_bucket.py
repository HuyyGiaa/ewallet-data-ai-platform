"""
Khởi tạo 3 bucket MinIO cho các zone Bronze / Silver / Gold.

Chạy từ máy host (không phải trong container), nên dùng localhost:9000
(khác với bên trong container Trino/Spark sẽ dùng minio:9000).

Cần cài trước: pip install minio
Chạy: python storage/scripts/init_bucket.py
"""

from minio import Minio
from minio.error import S3Error

BUCKETS = ["bronze-zone", "silver-zone", "gold-zone"]


def main():
    client = Minio(
        "localhost:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        secure=False,  # local dev, không dùng HTTPS
    )

    for bucket in BUCKETS:
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
                print(f"Đã tạo bucket: {bucket}")
            else:
                print(f"Bucket đã tồn tại: {bucket}")
        except S3Error as e:
            print(f"Lỗi khi tạo bucket {bucket}: {e}")


if __name__ == "__main__":
    main()