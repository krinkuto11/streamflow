"""Start required setup workers after Dispatcharr data becomes available."""

from functools import wraps
import threading
from typing import Any, Callable, Optional

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)
_processor_start_lock = threading.RLock()


def serialized_processor_start(start: Callable[..., Any]):
    """Keep each worker's alive check and thread creation atomic."""
    @wraps(start)
    def guarded(*args, **kwargs):
        with _processor_start_lock:
            return start(*args, **kwargs)
    return guarded


def start_setup_processors(
    *,
    is_configured: Callable[[], bool],
    start_scheduled_events: Callable[[], Any],
    start_epg_refresh: Callable[[], Any],
    start_udi_refresh: Callable[[], Any],
) -> None:
    """Start required workers without changing optional automation settings."""
    with _processor_start_lock:
        if not is_configured():
            return
        for name, start in (
            ("scheduled events", start_scheduled_events),
            ("EPG refresh", start_epg_refresh),
            ("UDI refresh", start_udi_refresh),
        ):
            try:
                start()
            except Exception:
                logger.exception("Failed to start %s after setup", name)


def initialize_setup_data(
    *,
    udi: Any,
    force_refresh: bool,
    on_initialized: Optional[Callable[[], Any]] = None,
) -> bool:
    """Initialize in a background thread, then start workers on live success."""
    try:
        if not udi.initialize(force_refresh=force_refresh):
            return False
        if on_initialized is not None and udi.is_network_ready():
            on_initialized()
        return True
    except Exception:
        logger.exception("Dispatcharr setup initialization failed")
        return False
