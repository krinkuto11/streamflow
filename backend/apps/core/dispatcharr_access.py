"""Shared, secret-free checks for Dispatcharr's administration permission."""

ADMIN_REQUIRED_MESSAGE = (
    "StreamFlow requires a Dispatcharr administrator (user level 10 or higher). "
    "Use an administrator API key or account so provider URLs and stream updates are available."
)


class DispatcharrAccessError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require_admin_user(payload):
    """Fail closed on an unreadable role; Dispatcharr authorizes by user_level."""
    level = payload.get("user_level") if isinstance(payload, dict) else None
    if isinstance(level, bool) or not isinstance(level, int):
        raise DispatcharrAccessError(
            "dispatcharr_permissions_unverified",
            "Could not verify Dispatcharr administrator permissions. Check the connection and account.",
        )
    if level < 10:
        raise DispatcharrAccessError("dispatcharr_admin_required", ADMIN_REQUIRED_MESSAGE)


def probe_admin_access(base_url, headers, *, get, timeout=10):
    response = get(
        f"{base_url.rstrip('/')}/api/accounts/users/me/",
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    require_admin_user(payload)
    return payload
