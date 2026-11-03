"""Network stream protocols accepted by profile routing and quality probes."""

STREAM_URL_PREFIXES = (
    'http://', 'https://', 'rtmp://', 'rtmps://', 'rtsp://', 'rtsps://',
)
RTSP_MIN_STARTUP_BUFFER_SECONDS = 60


def has_supported_stream_protocol(url: object) -> bool:
    return isinstance(url, str) and url.lower().startswith(STREAM_URL_PREFIXES)


def effective_stream_startup_buffer(url: str, configured: int) -> int:
    """Allow tuner setup before the media window, preserving larger settings."""
    if isinstance(url, str) and url.lower().startswith(('rtsp://', 'rtsps://')):
        return max(configured, RTSP_MIN_STARTUP_BUFFER_SECONDS)
    return configured
