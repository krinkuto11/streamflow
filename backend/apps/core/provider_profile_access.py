"""Current Dispatcharr user's provider-profile rights, separate from inventory.

The management API deliberately includes profiles this user cannot stream.
Keep them for viewer accounting, but never admit them as checker credentials.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import copy
import hashlib
import threading
import time

import requests

from apps.core.dispatcharr_access import probe_admin_access


@dataclass(frozen=True)
class ProfileAccess:
    verified: bool = False
    allowed_ids: frozenset | None = None
    identity: str = ""

    @classmethod
    def from_user(cls, user, identity=""):
        if not isinstance(user, dict):
            return cls(identity=identity)
        props = user.get("custom_properties")
        if props is None:
            props = {}
        if not isinstance(props, dict):
            return cls(identity=identity)
        ids = props.get("allowed_m3u_profile_ids")
        if ids is None:
            return cls(True, None, identity)
        if not isinstance(ids, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in ids
        ):
            return cls(identity=identity)
        return cls(True, frozenset(ids), identity)

    def allows(self, profile_id):
        return self.verified and (
            self.allowed_ids is None or profile_id in self.allowed_ids
        )

    def project_account(self, account):
        account = copy.deepcopy(account)
        if not isinstance(account, dict):
            return account
        account["_streamflow_profile_access"] = (
            self.verified, self.identity,
            None if self.allowed_ids is None else tuple(sorted(self.allowed_ids)),
        )
        account["_streamflow_raw_url_allowed"] = self.verified and self.allowed_ids is None
        for profile in account.get("profiles", []):
            if isinstance(profile, dict):
                original_active = profile.get("_dispatcharr_is_active", profile.get("is_active", True))
                profile["_dispatcharr_is_active"] = original_active
                profile["is_active"] = original_active
                profile["check_allowed"] = self.allows(profile.get("id"))
                if not profile["check_allowed"] and isinstance(original_active, bool):
                    profile["is_active"] = False
        return account


def account_access_reason(account):
    """Return a permission skip without confusing it with no configured profiles."""
    if not isinstance(account, dict) or "_streamflow_profile_access" not in account:
        return None  # Legacy embedders supply their own trusted inventory.
    if not account["_streamflow_profile_access"][0]:
        return "provider_permissions_unverified"
    if account.get("_streamflow_raw_url_allowed") is False and not any(
        isinstance(profile, dict) and profile.get("is_active", True)
        for profile in account.get("profiles", [])
    ):
        return "provider_profile_not_allowed"
    return None


class ProfileAccessCache:
    """Coalesce rights reads; never reuse rights for different credentials.

    Leases serialize publication with provider admission. Expired or failed
    reads do not fall back to a previously unrestricted policy.
    """

    ttl = 15.0
    failure_ttl = 2.0

    def __init__(self):
        self.lock = threading.RLock()
        self._key = None
        self._checked_at = 0.0
        self._expires_at = 0.0
        self._access = ProfileAccess()

    @staticmethod
    def _connection():
        from apps.core.auth import _get_auth_headers, _get_base_url
        from apps.config.dispatcharr_config import get_dispatcharr_config

        config = get_dispatcharr_config()
        base = (_get_base_url() or "").rstrip("/")
        headers = _get_auth_headers()
        material = (base, config.get_auth_mode(), config.get_username(),
                    config.get_password(), tuple(sorted(headers.items())))
        return base, headers, hashlib.sha256(repr(material).encode()).hexdigest()

    def _get_locked(self, force=False):
        try:
            base, headers, key = self._connection()
        except Exception:
            self._access = ProfileAccess()
            self._key = None
            return self._access
        now = time.monotonic()
        if key == self._key and now < self._expires_at and (
            not force or now - self._checked_at < 0.25
        ):
            return self._access
        access = ProfileAccess(identity=key)
        try:
            try:
                user = probe_admin_access(base, headers, get=requests.get, timeout=5)
            except requests.HTTPError as exc:
                from apps.core.auth import _refresh_token
                if exc.response is None or exc.response.status_code != 401 or not _refresh_token():
                    raise
                base, headers, key = self._connection()
                user = probe_admin_access(base, headers, get=requests.get, timeout=5)
            # A concurrent connection edit must not publish the old user's rights.
            if self._connection()[2] == key:
                access = ProfileAccess.from_user(user, key)
        except Exception:
            pass  # Return an explicit, secret-free unverified state.
        self._key = key
        self._access = access
        self._checked_at = time.monotonic()
        self._expires_at = self._checked_at + (self.ttl if access.verified else self.failure_ttl)
        return access

    def get(self, force=False):
        with self.lock:
            return self._get_locked(force)

    @contextmanager
    def lease(self):
        with self.lock:
            yield self._get_locked()


def checking_accounts(udi, accounts):
    """Project only checking callers; raw UDI inventory stays authoritative."""
    if getattr(type(udi), "supports_provider_profile_access", False) is True:
        access = udi.get_provider_profile_access()
        return [access.project_account(account) for account in accounts]
    return accounts


def checking_account(udi, account_id):
    account = udi.get_m3u_account_by_id(account_id)
    projected = checking_accounts(udi, [account])
    return projected[0]
