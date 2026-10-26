"""Small ownership boundary for bounded Spark persistence lifecycles."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def persisted(dataframe: Any, storage_level: Any) -> Iterator[Any]:
    cached = dataframe.persist(storage_level)
    try:
        yield cached
    finally:
        cached.unpersist(blocking=False)
