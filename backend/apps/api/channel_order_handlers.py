"""Channel order API handler functions extracted from web_api."""

from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_channel_order_response(*, get_channel_order_manager: Callable[[], Any]):
    """Handle reading the current custom channel order."""
    try:
        order_manager = get_channel_order_manager()
        order = order_manager.get_order()
        return jsonify({"order": order})
    except Exception as exc:
        logger.error(f"Error getting channel order: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def set_channel_order_response(*, payload: Any, get_channel_order_manager: Callable[[], Any]):
    """Handle updating custom channel order."""
    try:
        if not payload or "order" not in payload:
            return jsonify({"error": "Missing 'order' field in request"}), 400

        order = payload["order"]
        if not isinstance(order, list):
            return jsonify({"error": "'order' must be a list of channel IDs"}), 400

        if not all(isinstance(item, int) for item in order):
            return jsonify({"error": "'order' must contain only integer channel IDs"}), 400

        order_manager = get_channel_order_manager()
        success = order_manager.set_order(order)

        if success:
            return jsonify({"message": "Channel order updated successfully", "order": order})
        return jsonify({"error": "Failed to update channel order"}), 500
    except Exception as exc:
        logger.error(f"Error updating channel order: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def clear_channel_order_response(*, get_channel_order_manager: Callable[[], Any]):
    """Handle clearing custom channel order."""
    try:
        order_manager = get_channel_order_manager()
        success = order_manager.clear_order()

        if success:
            return jsonify({"message": "Channel order cleared successfully"})
        return jsonify({"error": "Failed to clear channel order"}), 500
    except Exception as exc:
        logger.error(f"Error clearing channel order: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
