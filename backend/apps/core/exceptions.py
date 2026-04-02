"""Common application exception types for API and service layers."""

from typing import Any, Dict, Optional


class StreamFlowError(Exception):
    """Base error carrying HTTP-safe metadata."""

    status_code = 500
    error_code = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code if status_code is not None else self.status_code
        self.error_code = error_code if error_code is not None else self.error_code
        self.details = details


class ValidationError(StreamFlowError):
    """Raised when request payload/query validation fails."""

    status_code = 400
    error_code = "validation_error"


class NotFoundError(StreamFlowError):
    """Raised when a requested resource cannot be found."""

    status_code = 404
    error_code = "not_found"


class ConflictError(StreamFlowError):
    """Raised when request conflicts with current resource state."""

    status_code = 409
    error_code = "conflict"
