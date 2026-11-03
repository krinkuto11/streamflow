"""Network stream protocols accepted by profile routing and quality probes."""

STREAM_URL_PREFIXES = (
    'http://', 'https://', 'rtmp://', 'rtmps://', 'rtsp://', 'rtsps://',
)


def has_supported_stream_protocol(url: object) -> bool:
    return isinstance(url, str) and url.lower().startswith(STREAM_URL_PREFIXES)
