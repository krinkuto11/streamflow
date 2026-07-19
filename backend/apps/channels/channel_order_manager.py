#!/usr/bin/env python3
"""
Channel Order Manager for StreamFlow.

Manages custom ordering of channels in the UI.
"""

import os
import threading
from pathlib import Path
from typing import List, Optional, Dict, Any

from apps.core.atomic_json import atomic_write_json, load_json_with_backup
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
CHANNEL_ORDER_FILE = CONFIG_DIR / 'channel_order.json'


class ChannelOrderManager:
    """
    Manages custom ordering of channels.
    
    Stores a list of channel IDs in the desired display order.
    Channels not in the list will appear after ordered channels.
    """
    
    def __init__(self):
        """Initialize the channel order manager."""
        self._lock = threading.Lock()
        self._channel_order: List[int] = []
        self._load_order()
        logger.info("Channel order manager initialized")
    
    def _load_order(self) -> None:
        """Load channel order from file."""
        try:
            data = load_json_with_backup(
                CHANNEL_ORDER_FILE,
                default=None,
                validator=lambda value: isinstance(value, dict),
            )
            if data is None:
                self._channel_order = []
                logger.info("No existing channel order file")
            else:
                self._channel_order = data.get('order', [])
                logger.info(f"Loaded order for {len(self._channel_order)} channels")
        except Exception as e:
            logger.error(f"Error loading channel order: {e}", exc_info=True)
            self._channel_order = []
    
    def _save_order(self) -> bool:
        """Save channel order to file.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            atomic_write_json(CHANNEL_ORDER_FILE, {'order': self._channel_order})
            logger.debug("Channel order saved successfully")
            return True
        except Exception as e:
            logger.error(f"Error saving channel order: {e}", exc_info=True)
            return False
    
    def get_order(self) -> List[int]:
        """Get the current channel order.
        
        Returns:
            List of channel IDs in display order
        """
        with self._lock:
            return self._channel_order.copy()
    
    def set_order(self, channel_ids: List[int]) -> bool:
        """Set the channel order.
        
        Args:
            channel_ids: List of channel IDs in desired order
            
        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            self._channel_order = channel_ids
            success = self._save_order()
            if success:
                logger.info(f"Updated channel order with {len(channel_ids)} channels")
            return success
    
    def apply_order(self, channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply custom ordering to a list of channel dictionaries.
        
        Channels in the custom order list will appear first in that order.
        Channels not in the order list will appear after, in their original order.
        
        Args:
            channels: List of channel dictionaries (must have 'id' field)
            
        Returns:
            Reordered list of channel dictionaries
        """
        with self._lock:
            if not self._channel_order:
                # No custom order, return as-is
                return channels
            
            # Create lookup for channels
            channel_map = {ch['id']: ch for ch in channels if 'id' in ch}
            
            # Build result list
            result = []
            
            # First, add channels in custom order
            for channel_id in self._channel_order:
                if channel_id in channel_map:
                    result.append(channel_map[channel_id])
                    del channel_map[channel_id]  # Remove from map to avoid duplicates
            
            # Then add remaining channels that weren't in the custom order
            remaining = [ch for ch in channels if ch.get('id') in channel_map]
            result.extend(remaining)
            
            return result
    
    def clear_order(self) -> bool:
        """Clear the custom channel order.
        
        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            self._channel_order = []
            success = self._save_order()
            if success:
                logger.info("Cleared channel order")
            return success


# Global singleton instance
_channel_order_manager: Optional[ChannelOrderManager] = None
_order_lock = threading.Lock()


def get_channel_order_manager() -> ChannelOrderManager:
    """Get the global channel order manager singleton instance.
    
    Returns:
        The channel order manager instance
    """
    global _channel_order_manager
    with _order_lock:
        if _channel_order_manager is None:
            _channel_order_manager = ChannelOrderManager()
        return _channel_order_manager
