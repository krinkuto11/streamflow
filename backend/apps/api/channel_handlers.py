"""Channel API handler functions extracted from web_api."""

import os
from pathlib import Path
from typing import Any, Callable, Dict

import requests
from flask import jsonify, send_file

from apps.channels.logo_cache import InvalidLogoResponse, download_and_cache_logo, find_cached_logo
from apps.channels.service import ChannelQuery
from apps.core.api_responses import error_response
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

def _send_cached_logo(path: Path):
    ext = path.suffix
    mimetype = "image/svg+xml" if ext == ".svg" else f"image/{'jpeg' if ext in ('.jpg', '.jpeg') else ext[1:]}"
    response = send_file(path, mimetype=mimetype)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if ext == ".svg":
        response.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
        )
    return response


def get_channels_response(
    *,
    request_args: Any,
    parse_pagination_params: Callable[..., Any],
    get_channel_service: Callable[[], Any],
):
    """Handle channel listing with filtering, sorting, and pagination."""
    try:
        search = request_args.get("search", "").strip()
        sort_by = request_args.get("sort_by", "name")
        sort_dir = request_args.get("sort_dir", "asc")
        page_param = request_args.get("page", None)
        per_page_param = request_args.get("per_page", "50")

        page, per_page, err = parse_pagination_params(page_param, per_page_param)
        if err:
            return err

        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"

        result = get_channel_service().list_channels(
            ChannelQuery(
                search=search,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                per_page=per_page,
            )
        )

        if "error" in result:
            return error_response(
                result["error"],
                status_code=result.get("status", 500),
                code="channels_fetch_failed",
            )

        if not result.get("paginated", False):
            return jsonify(result["items"])

        return jsonify(
            {
                "items": result["items"],
                "total": result["total"],
                "page": result["page"],
                "per_page": result["per_page"],
                "total_pages": result["total_pages"],
                "has_next": result["has_next"],
                "has_prev": result["has_prev"],
            }
        )
    except Exception as exc:
        logger.error(f"Error fetching channels: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_channel_stats_response(
    *,
    channel_id: str,
    get_channel_service: Callable[[], Any],
):
    """Handle channel statistics lookup."""
    try:
        try:
            channel_id_int = int(channel_id)
        except (ValueError, TypeError):
            return error_response(
                "Invalid channel ID: must be a valid integer",
                status_code=400,
                code="invalid_channel_id",
            )

        result = get_channel_service().get_channel_stats(channel_id_int)
        if "error" in result:
            return error_response(
                result["error"],
                status_code=result.get("status", 500),
                code="channel_stats_failed",
            )

        return jsonify(result["data"])
    except Exception as exc:
        logger.error(f"Error fetching channel stats: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_channel_groups_response(*, get_udi_manager: Callable[[], Any]):
    """Handle channel group listing."""
    try:
        udi = get_udi_manager()
        groups = udi.get_channel_groups()

        if groups is None:
            return jsonify({"error": "Failed to fetch channel groups"}), 500

        return jsonify(groups)
    except Exception as exc:
        logger.error(f"Error fetching channel groups: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_channel_logo_response(*, logo_id: str, get_udi_manager: Callable[[], Any]):
    """Handle logo object lookup from UDI."""
    try:
        udi = get_udi_manager()
        logo = udi.get_logo_by_id(int(logo_id))

        if logo is None:
            return jsonify({"error": "Failed to fetch logo"}), 500

        return jsonify(logo)
    except Exception as exc:
        logger.error(f"Error fetching logo: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_channel_logo_cached_response(
    *,
    logo_id: str,
    config_dir: Path,
    get_udi_manager: Callable[[], Any],
    get_dispatcharr_config: Callable[[], Any],
):
    """Download and cache channel logo locally, then serve it."""
    try:
        try:
            logo_id_int = int(logo_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid logo ID: must be a valid integer"}), 400

        if logo_id_int <= 0:
            return jsonify({"error": "Invalid logo ID: must be a positive integer"}), 400

        logos_cache_dir = config_dir / "logos_cache"
        cached_path = find_cached_logo(logos_cache_dir, logo_id_int)
        if cached_path is not None:
            return _send_cached_logo(cached_path)

        udi = get_udi_manager()
        logo = udi.get_logo_by_id(logo_id_int)
        if not logo:
            return jsonify({"error": "Logo not found"}), 404

        dispatcharr_config = get_dispatcharr_config()
        dispatcharr_base_url = dispatcharr_config.get_base_url()
        if not dispatcharr_base_url:
            dispatcharr_base_url = os.getenv("DISPATCHARR_BASE_URL", "")
            if not dispatcharr_base_url:
                return jsonify({"error": "DISPATCHARR_BASE_URL not configured"}), 500

        logo_url = logo.get("cache_url") or logo.get("url")
        if not logo_url:
            return jsonify({"error": "Logo URL not available"}), 404

        logger.debug("Downloading logo %s", logo_id_int)
        cached_path = download_and_cache_logo(logos_cache_dir, logo_id_int, logo_url, dispatcharr_base_url)

        logger.debug(f"Cached logo {logo_id} to {cached_path}")

        return _send_cached_logo(cached_path)

    except InvalidLogoResponse as exc:
        logger.warning("Rejected logo %s: %s", logo_id, exc)
        return jsonify({"error": "Logo response was rejected"}), 422
    except requests.exceptions.RequestException as exc:
        logger.error(f"Error downloading logo {logo_id}: {exc}")
        return jsonify({"error": "Failed to download logo"}), 500
    except Exception as exc:
        logger.error(f"Error caching logo {logo_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
