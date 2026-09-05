"""
Đọc dữ liệu offline và ghi thành Delta Lake tables trên MinIO.

Bronze Layer giữ nguyên cấu trúc nghiệp vụ của dữ liệu nguồn:
- Không loại duplicate.
- Không xử lý null.
- Không thêm cột partition.
- Không làm sạch dữ liệu.

Chạy file này gián tiếp thông qua init_storage.py hoặc trực tiếp để kiểm thử.
"""

from __future__ import annotations
import gc
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from config import (
    BRONZE_BUCKET,
    DELTA_STORAGE_OPTIONS,
    OFFLINE_TABLES,
    TABLE_DATE_COLUMNS,
    TABLE_DATETIME_COLUMNS,
    TABLE_STRING_COLUMNS,
    delta_table_uri,
    parquet_path,
)


logger = logging.getLogger(__name__)

WriteMode = Literal["overwrite", "append"]


class DeltaWriterError(RuntimeError):
    """Lỗi xảy ra trong quá trình đọc hoặc ghi Delta Lake."""


@dataclass(frozen=True)
class DeltaWriteResult:
    """Kết quả ghi một bảng Delta Lake."""

    table_name: str
    source_path: Path
    table_uri: str
    source_rows: int
    delta_rows: int
    delta_version: int


def validate_source_file(table_name: str, source_path: Path) -> None:
    """
    Kiểm tra file nguồn trước khi đọc.

    Raises:
        DeltaWriterError: Nếu file không tồn tại hoặc không phải file.
    """
    if not source_path.exists():
        raise DeltaWriterError(
            f"Không tìm thấy file nguồn của bảng '{table_name}': {source_path}"
        )

    if not source_path.is_file():
        raise DeltaWriterError(
            f"Đường dẫn nguồn không phải file: {source_path}"
        )

    if source_path.stat().st_size == 0:
        raise DeltaWriterError(
            f"File nguồn rỗng: {source_path}"
        )


def read_offline_table(table_name: str,source_path: Path | None = None,) -> pd.DataFrame:
    if source_path is None:
        source_path = parquet_path(table_name)

    validate_source_file(table_name, source_path)

    logger.info(
        "[%s] Đang đọc file nguồn: %s",
        table_name,
        source_path,
    )

    try:
        dataframe = pd.read_parquet(source_path)
    except Exception as exc:
        raise DeltaWriterError(
            f"Không thể đọc Parquet của bảng '{table_name}': {exc}"
        ) from exc

    if dataframe.empty:
        logger.warning(
            "[%s] File nguồn không có dòng dữ liệu.",
            table_name,
        )
    else:
        logger.info(
            "[%s] Đọc thành công %s dòng, %d cột.",
            table_name,
            f"{len(dataframe):,}",
            len(dataframe.columns),
        )

    return dataframe


def validate_expected_columns(dataframe: pd.DataFrame, table_name: str, allowed_missing_columns: tuple[str, ...] = (),) -> None:
    configured_columns = set(
        TABLE_STRING_COLUMNS.get(table_name, ())
        + TABLE_DATETIME_COLUMNS.get(table_name, ())
        + TABLE_DATE_COLUMNS.get(table_name, ())
    )

    missing_columns = (
        configured_columns
        .difference(dataframe.columns)
        .difference(allowed_missing_columns)
    )

    if missing_columns:
        missing_text = ", ".join(sorted(missing_columns))
        raise DeltaWriterError(
            f"Bảng '{table_name}' thiếu các cột mong đợi: "
            f"{missing_text}"
        )


def normalize_dataframe(dataframe: pd.DataFrame, table_name: str, allowed_missing_columns: tuple[str, ...] = (),) -> pd.DataFrame:
    """
    Chuẩn hóa kiểu dữ liệu trước khi chuyển sang Arrow.

    Không thay đổi giá trị nghiệp vụ.
    """
    normalized = dataframe.copy()

    validate_expected_columns(
        normalized,
        table_name,
        allowed_missing_columns,
    )

    for column in TABLE_STRING_COLUMNS.get(table_name, ()):
        if column in normalized.columns:
            normalized[column] = (
                normalized[column].astype("string")
            )

    for column in TABLE_DATETIME_COLUMNS.get(table_name, ()):
        if column not in normalized.columns:
            continue

        try:
            normalized[column] = pd.to_datetime(
                normalized[column],
                errors="raise",
            )
        except Exception as exc:
            raise DeltaWriterError(
                f"Bảng '{table_name}', cột '{column}' "
                f"không thể chuyển thành timestamp: {exc}"
            ) from exc

    for column in TABLE_DATE_COLUMNS.get(table_name, ()):
        if column not in normalized.columns:
            continue

        try:
            normalized[column] = pd.to_datetime(
                normalized[column],
                errors="raise",
            ).dt.date
        except Exception as exc:
            raise DeltaWriterError(
                f"Bảng '{table_name}', cột '{column}' "
                f"không thể chuyển thành date: {exc}"
            ) from exc

    return normalized


def dataframe_to_arrow(dataframe: pd.DataFrame, table_name: str,) -> pa.Table:
    """Chuyển Pandas DataFrame sang PyArrow Table."""
    try:
        arrow_table = pa.Table.from_pandas(
            dataframe,
            preserve_index=False,
        )
    except Exception as exc:
        raise DeltaWriterError(
            f"Không thể chuyển bảng '{table_name}' sang PyArrow: {exc}"
        ) from exc

    logger.info(
        "[%s] Arrow schema:\n%s",
        table_name,
        arrow_table.schema,
    )

    return arrow_table


def get_delta_row_count(delta_table: DeltaTable) -> int:
    """
    Đếm số dòng hiện tại của Delta table.

    Dùng PyArrow Dataset để tránh chuyển toàn bộ dữ liệu sang Pandas.
    """
    try:
        dataset = delta_table.to_pyarrow_dataset()
        return int(dataset.count_rows())
    except Exception as exc:
        raise DeltaWriterError(
            f"Không thể đếm dữ liệu Delta table: {exc}"
        ) from exc


def write_transaction_schema_evolution(bucket: str,) -> DeltaWriteResult:
    table_name = "transactions"
    table_uri = delta_table_uri(bucket, table_name)

    source_dir = parquet_path(table_name).parent
    v1_path = source_dir / "transactions_v1.parquet"
    v2_path = source_dir / "transactions_v2.parquet"

    validate_source_file(table_name, v1_path)
    validate_source_file(table_name, v2_path)

    logger.info(
        "[transactions] Schema evolution: V1 -> V2."
    )

    # V1
    v1_df = read_offline_table(
        table_name,
        source_path=v1_path,
    )

    v1_df = normalize_dataframe(
        v1_df,
        table_name,
        allowed_missing_columns=("channel",),
    )

    if "channel" in v1_df.columns:
        raise DeltaWriterError(
            "transactions_v1 không được chứa cột 'channel'."
        )

    v1_rows = len(v1_df)

    v1_arrow = dataframe_to_arrow(
        v1_df,
        "transactions_v1",
    )

    logger.info(
        "[transactions] Ghi V1: %s rows, không có channel.",
        f"{v1_rows:,}",
    )

    try:
        write_deltalake(
            table_or_uri=table_uri,
            data=v1_arrow,
            mode="overwrite",
            schema_mode="overwrite",
            engine="rust",
            storage_options=DELTA_STORAGE_OPTIONS,
        )
    except Exception as exc:
        raise DeltaWriterError(
            f"Không thể ghi transactions V1: {exc}"
        ) from exc

    delta_after_v1 = DeltaTable(
        table_uri,
        storage_options=DELTA_STORAGE_OPTIONS,
    )

    logger.info(
        "[transactions] V1 hoàn tất: "
        "version=%d | channel=False.",
        delta_after_v1.version(),
    )

    del v1_arrow
    del v1_df
    del delta_after_v1
    gc.collect()

    logger.info(
        "[transactions] Đã giải phóng V1 khỏi memory."
    )

    # V2 chỉ được đọc sau khi V1 đã giải phóng
    v2_df = read_offline_table(
        table_name,
        source_path=v2_path,
    )

    v2_df = normalize_dataframe(
        v2_df,
        table_name,
    )

    if "channel" not in v2_df.columns:
        raise DeltaWriterError(
            "transactions_v2 phải chứa cột 'channel'."
        )

    v2_rows = len(v2_df)

    v2_arrow = dataframe_to_arrow(
        v2_df,
        "transactions_v2",
    )

    logger.info(
        "[transactions] Append V2: %s rows, "
        "schema mới có channel.",
        f"{v2_rows:,}",
    )

    try:
        write_deltalake(
            table_or_uri=table_uri,
            data=v2_arrow,
            mode="append",
            schema_mode="merge",
            engine="rust",
            storage_options=DELTA_STORAGE_OPTIONS,
        )
    except Exception as exc:
        raise DeltaWriterError(
            f"Không thể ghi transactions V2: {exc}"
        ) from exc

    del v2_arrow
    del v2_df
    gc.collect()

    delta_table = DeltaTable(
        table_uri,
        storage_options=DELTA_STORAGE_OPTIONS,
    )

    delta_rows = get_delta_row_count(delta_table)
    delta_version = delta_table.version()

    source_rows = v1_rows + v2_rows

    if delta_rows != source_rows:
        raise DeltaWriterError(
            "Transactions ghi không khớp số dòng: "
            f"source={source_rows}, delta={delta_rows}"
        )

    logger.info(
        "[transactions] Schema evolution thành công: "
        "V1=%s | V2=%s | total=%s | version=%d.",
        f"{v1_rows:,}",
        f"{v2_rows:,}",
        f"{delta_rows:,}",
        delta_version,
    )

    return DeltaWriteResult(
        table_name=table_name,
        source_path=v1_path,
        table_uri=table_uri,
        source_rows=source_rows,
        delta_rows=delta_rows,
        delta_version=delta_version,
    )

def write_delta_table(table_name: str, bucket: str = BRONZE_BUCKET, mode: WriteMode = "overwrite",) -> DeltaWriteResult:
    """
    Đọc một file offline và ghi thành Delta table trên MinIO.

    Args:
        table_name:
            Tên bảng, đồng thời là tên file Parquet không có phần mở rộng.

        bucket:
            Bucket đích trên MinIO.

        mode:
            overwrite: ghi lại toàn bộ bảng, phù hợp cho bootstrap offline.
            append: bổ sung dữ liệu vào bảng hiện có.

    Returns:
        DeltaWriteResult chứa thông tin xác minh sau khi ghi.
    """
    if table_name == "transactions":
        if mode != "overwrite":
            raise DeltaWriterError(
                "Bootstrap transactions với schema evolution "
                "chỉ hỗ trợ mode='overwrite'."
            )

        return write_transaction_schema_evolution(
            bucket=bucket,
        )    
    source_path = parquet_path(table_name)
    table_uri = delta_table_uri(bucket, table_name)

    dataframe = read_offline_table(table_name)
    normalized_dataframe = normalize_dataframe(dataframe, table_name)
    arrow_table = dataframe_to_arrow(normalized_dataframe, table_name)

    logger.info(
        "[%s] Bắt đầu ghi Delta Lake vào %s với mode=%s.",
        table_name,
        table_uri,
        mode,
    )

    try:
        write_options = {
            "table_or_uri": table_uri,
            "data": arrow_table,
            "mode": mode,
            "storage_options": DELTA_STORAGE_OPTIONS,
            "engine": "rust",
        }

        # Bootstrap chạy lại phải chấp nhận schema nguồn hiện tại.
        if mode == "overwrite":
            write_options["schema_mode"] = "overwrite"

        write_deltalake(**write_options)

    except Exception as exc:
        raise DeltaWriterError(
            f"Không thể ghi bảng '{table_name}' vào '{table_uri}': {exc}"
        ) from exc

    try:
        delta_table = DeltaTable(
            table_uri,
            storage_options=DELTA_STORAGE_OPTIONS,
        )

        delta_rows = get_delta_row_count(delta_table)
        delta_version = delta_table.version()

    except Exception as exc:
        raise DeltaWriterError(
            f"Đã ghi nhưng không thể mở lại Delta table "
            f"'{table_name}': {exc}"
        ) from exc

    source_rows = len(normalized_dataframe)

    if delta_rows != source_rows and mode == "overwrite":
        raise DeltaWriterError(
            f"Bảng '{table_name}' ghi không khớp số dòng: "
            f"source={source_rows}, delta={delta_rows}"
        )

    logger.info(
        "[%s] Ghi Delta thành công: %s dòng, version=%d.",
        table_name,
        f"{delta_rows:,}",
        delta_version,
    )

    return DeltaWriteResult(
        table_name=table_name,
        source_path=source_path,
        table_uri=table_uri,
        source_rows=source_rows,
        delta_rows=delta_rows,
        delta_version=delta_version,
    )


def write_all_offline_tables(mode: WriteMode = "overwrite",) -> list[DeltaWriteResult]:
    """
    Ghi toàn bộ bảng offline vào Bronze Layer.

    Hàm dừng ngay khi một bảng thất bại để tránh trạng thái bootstrap
    có vẻ thành công nhưng thực tế chỉ ghi được một phần.
    """
    results: list[DeltaWriteResult] = []

    logger.info(
        "Bắt đầu ghi %d bảng offline vào bucket '%s'.",
        len(OFFLINE_TABLES),
        BRONZE_BUCKET,
    )

    for position, table_name in enumerate(OFFLINE_TABLES, start=1):
        logger.info(
            "========== [%d/%d] %s ==========",
            position,
            len(OFFLINE_TABLES),
            table_name,
        )

        result = write_delta_table(
            table_name=table_name,
            bucket=BRONZE_BUCKET,
            mode=mode,
        )
        results.append(result)

    logger.info(
        "Hoàn tất ghi %d/%d bảng vào Bronze Layer.",
        len(results),
        len(OFFLINE_TABLES),
    )

    return results