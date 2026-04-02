"""Shared response helpers for consistent API payloads."""

from typing import Any, Dict, Optional

from flask import jsonify


def success_response(
    data: Optional[Any] = None,
    *,
    message: Optional[str] = None,
    status_code: int = 200,
    meta: Optional[Dict[str, Any]] = None,
):
    """Return a consistent success envelope."""
    payload: Dict[str, Any] = {"success": True}

    if message is not None:
        payload["message"] = message
    if data is not None:
        payload["data"] = data
    if meta is not None:
        payload["meta"] = meta

    return jsonify(payload), status_code


def error_response(
    message: str,
    *,
    status_code: int = 400,
    code: str = "bad_request",
    details: Optional[Dict[str, Any]] = None,
):
    """Return a consistent error payload while preserving legacy ``error`` usage."""
    payload: Dict[str, Any] = {
        "success": False,
        "error": message,
        "code": code,
    }
    if details is not None:
        payload["details"] = details

    return jsonify(payload), status_code
