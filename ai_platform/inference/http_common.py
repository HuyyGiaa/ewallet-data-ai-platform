"""HTTP helpers shared by the Fraud API and the Feature API."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi.responses import JSONResponse

from ai_platform.inference.schemas import ErrorBody, ErrorResponse

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def normalize_correlation_id(value: str | None) -> str:
    """Keep a well-formed client correlation id, otherwise create one."""
    if value is not None and _CORRELATION_ID.fullmatch(value):
        return value
    return str(uuid4())


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    correlation_id: str,
    retryable: bool = False,
    details: tuple[dict[str, object], ...] = (),
) -> JSONResponse:
    """Build the sanitized error body both APIs return."""
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            correlation_id=correlation_id,
            retryable=retryable,
            details=details,
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))
