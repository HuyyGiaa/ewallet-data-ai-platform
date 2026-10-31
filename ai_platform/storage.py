"""Small URI helpers for immutable local or GCS AI-platform artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pyarrow.dataset as ds
import pyarrow.fs as fs


def normalize_uri(value: str | Path) -> str:
    text = str(value)
    if "://" in text:
        return text.rstrip("/")
    return str(Path(text).resolve())


def join_uri(root: str | Path, *parts: str) -> str:
    return "/".join(
        [normalize_uri(root), *(part.strip("/") for part in parts if part)]
    )


def filesystem_path(uri: str | Path) -> tuple[fs.FileSystem, str]:
    return fs.FileSystem.from_uri(normalize_uri(uri))


def require_absent(uri: str | Path) -> None:
    filesystem, path = filesystem_path(uri)
    info = filesystem.get_file_info(path)
    children = filesystem.get_file_info(
        fs.FileSelector(path, recursive=True, allow_not_found=True)
    )
    if info.type != fs.FileType.NotFound or children:
        raise FileExistsError(f"immutable output already exists: {normalize_uri(uri)}")


def reset_directory(uri: str | Path) -> tuple[fs.FileSystem, str]:
    filesystem, path = filesystem_path(uri)
    info = filesystem.get_file_info(path)
    if info.type != fs.FileType.NotFound:
        filesystem.delete_dir(path)
    filesystem.create_dir(path, recursive=True)
    return filesystem, path


def write_json(uri: str | Path, payload: Mapping[str, Any]) -> None:
    filesystem, path = filesystem_path(uri)
    filesystem.create_dir(str(Path(path).parent), recursive=True)
    with filesystem.open_output_stream(path) as stream:
        stream.write((json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def write_pandas_parquet(frame: Any, uri: str | Path) -> None:
    filesystem, path = filesystem_path(uri)
    filesystem.create_dir(str(Path(path).parent), recursive=True)
    with filesystem.open_output_stream(path) as stream:
        frame.to_parquet(stream, index=False)


def parquet_row_count(uri: str | Path) -> int:
    filesystem, path = filesystem_path(uri)
    return ds.dataset(path, filesystem=filesystem, format="parquet").count_rows()
