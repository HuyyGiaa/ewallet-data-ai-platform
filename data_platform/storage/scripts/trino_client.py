"""
Trino client cho Storage Layer.

Chức năng:
- Kiểm tra kết nối Trino.
- Tạo schema nếu chưa tồn tại.
- Kiểm tra bảng đã được đăng ký hay chưa.
- Đăng ký Delta table bằng system.register_table.
- Xác minh bảng bằng COUNT(*).
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generator

from trino.dbapi import Connection, connect
from trino.exceptions import Error

from data_platform.storage.scripts.config import (
    BRONZE_BUCKET,
    BRONZE_SCHEMA,
    TRINO_CATALOG,
    TRINO_HOST,
    TRINO_PORT,
    TRINO_USER,
    delta_table_uri,
)


logger = logging.getLogger(__name__)


class TrinoClientError(RuntimeError):
    """Lỗi xảy ra khi giao tiếp với Trino."""


@dataclass(frozen=True)
class TrinoTableResult:
    """Kết quả đăng ký và kiểm tra một bảng trong Trino."""

    catalog: str
    schema: str
    table: str
    location: str
    row_count: int
    registered_now: bool


def validate_identifier(identifier: str) -> str:
    """
    Kiểm tra identifier trước khi đưa vào câu SQL.

    Project chỉ sử dụng tên gồm chữ cái, số và dấu gạch dưới.
    """
    if not identifier:
        raise ValueError("Identifier không được để trống.")

    if not identifier.replace("_", "").isalnum():
        raise ValueError(
            f"Identifier không hợp lệ: {identifier!r}. "
            "Chỉ chấp nhận chữ, số và dấu gạch dưới."
        )

    return identifier


def escape_sql_string(value: str) -> str:
    """Escape chuỗi dùng trong SQL literal."""
    return value.replace("'", "''")


def create_trino_connection() -> Connection:
    """Tạo kết nối tới Trino chạy trên máy host."""
    logger.info(
        "Kết nối Trino tại %s:%d, catalog=%s.",
        TRINO_HOST,
        TRINO_PORT,
        TRINO_CATALOG,
    )

    try:
        return connect(
            host=TRINO_HOST,
            port=TRINO_PORT,
            user=TRINO_USER,
            catalog=TRINO_CATALOG,
            http_scheme="http",
        )
    except Exception as exc:
        raise TrinoClientError(
            f"Không thể tạo kết nối Trino tại "
            f"{TRINO_HOST}:{TRINO_PORT}: {exc}"
        ) from exc


@contextmanager
def trino_cursor() -> Generator[Any, None, None]:
    """
    Context manager đóng cursor và connection sau khi sử dụng.
    """
    connection: Connection | None = None
    cursor = None

    try:
        connection = create_trino_connection()
        cursor = connection.cursor()
        yield cursor

    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                logger.warning(
                    "Không thể đóng Trino cursor.",
                    exc_info=True,
                )

        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.warning(
                    "Không thể đóng kết nối Trino.",
                    exc_info=True,
                )


def execute_sql(
    cursor: Any,
    sql: str,
    fetch: bool = False,
) -> list[tuple]:
    """
    Thực thi một câu SQL trên Trino.

    Args:
        cursor:
            Trino DBAPI cursor.

        sql:
            Câu SQL cần thực thi.

        fetch:
            Có lấy kết quả bằng fetchall() hay không.
    """
    logger.debug("SQL:\n%s", sql)

    try:
        cursor.execute(sql)

        if fetch:
            return list(cursor.fetchall())

        return []

    except Error as exc:
        raise TrinoClientError(
            f"Trino không thể thực thi SQL:\n{sql}\nLỗi: {exc}"
        ) from exc
    except Exception as exc:
        raise TrinoClientError(
            f"Lỗi khi thực thi SQL trên Trino:\n{sql}\nLỗi: {exc}"
        ) from exc


def check_trino_connection(cursor: Any) -> None:
    """Kiểm tra Trino bằng một truy vấn đơn giản."""
    rows = execute_sql(
        cursor,
        "SELECT 1",
        fetch=True,
    )

    if not rows or rows[0][0] != 1:
        raise TrinoClientError(
            f"Trino trả về kết quả kiểm tra không hợp lệ: {rows}"
        )

    logger.info("Kết nối Trino thành công.")


def ensure_schema(
    cursor: Any,
    schema_name: str,
    bucket_name: str,
) -> None:
    """
    Tạo schema nếu chưa tồn tại.

    Schema location trỏ tới root bucket tương ứng.
    """
    schema_name = validate_identifier(schema_name)
    catalog_name = validate_identifier(TRINO_CATALOG)

    schema_location = f"s3://{bucket_name}/"

    sql = f"""
    CREATE SCHEMA IF NOT EXISTS
        {catalog_name}.{schema_name}
    WITH (
        location = '{escape_sql_string(schema_location)}'
    )
    """

    execute_sql(cursor, sql)

    logger.info(
        "Schema sẵn sàng: %s.%s",
        catalog_name,
        schema_name,
    )


def table_exists(
    cursor: Any,
    schema_name: str,
    table_name: str,
) -> bool:
    """Kiểm tra bảng đã tồn tại trong Trino metastore hay chưa."""
    schema_name = validate_identifier(schema_name)
    table_name = validate_identifier(table_name)
    catalog_name = validate_identifier(TRINO_CATALOG)

    sql = f"""
    SELECT COUNT(*)
    FROM {catalog_name}.information_schema.tables
    WHERE table_schema = '{escape_sql_string(schema_name)}'
      AND table_name = '{escape_sql_string(table_name)}'
    """

    rows = execute_sql(cursor, sql, fetch=True)

    return bool(rows and int(rows[0][0]) > 0)


def normalize_table_location(location: str) -> str:
    """Chuẩn hóa location để so sánh mà không phụ thuộc dấu slash cuối."""
    return location.rstrip("/")


def get_table_location(
    cursor: Any,
    schema_name: str,
    table_name: str,
) -> str | None:
    """Đọc location hiện tại của một table từ SHOW CREATE TABLE."""
    schema_name = validate_identifier(schema_name)
    table_name = validate_identifier(table_name)
    catalog_name = validate_identifier(TRINO_CATALOG)

    if not table_exists(cursor, schema_name, table_name):
        return None

    rows = execute_sql(
        cursor,
        (
            f"SHOW CREATE TABLE "
            f"{catalog_name}.{schema_name}.{table_name}"
        ),
        fetch=True,
    )

    if not rows:
        raise TrinoClientError(
            "Không nhận được DDL cho bảng "
            f"{catalog_name}.{schema_name}.{table_name}."
        )

    match = re.search(
        r"(?m)^\s*location\s*=\s*'((?:[^']|'')*)'\s*,?\s*$",
        str(rows[0][0]),
    )

    if match is None:
        raise TrinoClientError(
            "Không xác định được location của bảng "
            f"{catalog_name}.{schema_name}.{table_name} từ SHOW CREATE TABLE."
        )

    return match.group(1).replace("''", "'")


def list_schema_tables(
    cursor: Any,
    schema_name: str,
) -> set[str]:
    """Liệt kê table hiện có trong một Trino schema."""
    schema_name = validate_identifier(schema_name)
    catalog_name = validate_identifier(TRINO_CATALOG)
    rows = execute_sql(
        cursor,
        f"SHOW TABLES FROM {catalog_name}.{schema_name}",
        fetch=True,
    )
    return {str(row[0]) for row in rows}


def verify_table_readable(
    cursor: Any,
    schema_name: str,
    table_name: str,
) -> None:
    """Buộc Trino lập kế hoạch và đọc tối đa một dòng của table."""
    schema_name = validate_identifier(schema_name)
    table_name = validate_identifier(table_name)
    catalog_name = validate_identifier(TRINO_CATALOG)
    execute_sql(
        cursor,
        (
            f"SELECT 1 FROM "
            f"{catalog_name}.{schema_name}.{table_name} LIMIT 1"
        ),
        fetch=True,
    )


def register_delta_table(
    cursor: Any,
    table_name: str,
    schema_name: str = BRONZE_SCHEMA,
    bucket_name: str = BRONZE_BUCKET,
) -> bool:
    """
    Đăng ký một Delta table đã tồn tại vào Trino.

    Returns:
        True nếu bảng vừa được đăng ký.
        False nếu bảng đã tồn tại trong metastore.
    """
    schema_name = validate_identifier(schema_name)
    table_name = validate_identifier(table_name)
    catalog_name = validate_identifier(TRINO_CATALOG)

    table_location = delta_table_uri(bucket_name, table_name)

    if table_exists(cursor, schema_name, table_name):
        existing_location = get_table_location(
            cursor,
            schema_name,
            table_name,
        )

        if normalize_table_location(existing_location or "") != (
            normalize_table_location(table_location)
        ):
            raise TrinoClientError(
                "LOCATION MISMATCH cho bảng "
                f"{catalog_name}.{schema_name}.{table_name}: "
                f"actual={existing_location!r}, "
                f"expected={table_location!r}"
            )

        logger.info(
            "ALREADY PRESENT: %s.%s.%s -> %s",
            catalog_name,
            schema_name,
            table_name,
            existing_location,
        )
        return False

    sql = f"""
    CALL {catalog_name}.system.register_table(
        schema_name => '{escape_sql_string(schema_name)}',
        table_name => '{escape_sql_string(table_name)}',
        table_location => '{escape_sql_string(table_location)}'
    )
    """

    execute_sql(cursor, sql)

    logger.info(
        "Đăng ký bảng thành công: %s.%s.%s -> %s",
        catalog_name,
        schema_name,
        table_name,
        table_location,
    )

    return True


def count_table_rows(
    cursor: Any,
    table_name: str,
    schema_name: str = BRONZE_SCHEMA,
) -> int:
    """Đếm số dòng của bảng qua Trino."""
    schema_name = validate_identifier(schema_name)
    table_name = validate_identifier(table_name)
    catalog_name = validate_identifier(TRINO_CATALOG)

    sql = f"""
    SELECT COUNT(*)
    FROM {catalog_name}.{schema_name}.{table_name}
    """

    rows = execute_sql(cursor, sql, fetch=True)

    if not rows:
        raise TrinoClientError(
            f"Không nhận được kết quả COUNT(*) cho bảng "
            f"{catalog_name}.{schema_name}.{table_name}"
        )

    row_count = int(rows[0][0])

    logger.info(
        "Trino xác minh bảng %s.%s.%s: %s dòng.",
        catalog_name,
        schema_name,
        table_name,
        f"{row_count:,}",
    )

    return row_count


def register_and_verify_table(
    cursor: Any,
    table_name: str,
    expected_rows: int | None = None,
    schema_name: str = BRONZE_SCHEMA,
    bucket_name: str = BRONZE_BUCKET,
    ensure_schema_exists: bool = True,
) -> TrinoTableResult:
    """
    Đăng ký Delta table và xác minh số dòng qua Trino.
    """
    if ensure_schema_exists:
        ensure_schema(
            cursor=cursor,
            schema_name=schema_name,
            bucket_name=bucket_name,
        )

    registered_now = register_delta_table(
        cursor=cursor,
        table_name=table_name,
        schema_name=schema_name,
        bucket_name=bucket_name,
    )

    row_count = count_table_rows(
        cursor=cursor,
        table_name=table_name,
        schema_name=schema_name,
    )

    if expected_rows is not None and row_count != expected_rows:
        raise TrinoClientError(
            f"Số dòng không khớp cho bảng '{table_name}': "
            f"expected={expected_rows}, Trino={row_count}"
        )

    return TrinoTableResult(
        catalog=TRINO_CATALOG,
        schema=schema_name,
        table=table_name,
        location=delta_table_uri(bucket_name, table_name),
        row_count=row_count,
        registered_now=registered_now,
    )
