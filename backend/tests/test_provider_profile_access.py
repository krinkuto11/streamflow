"""Issue 542: user permissions govern probes, not management API visibility."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
import requests

from apps.core.provider_profile_access import ProfileAccess, ProfileAccessCache, checking_accounts
from apps.stream import concurrent_stream_limiter as limits
from apps.stream.checker.sequential_channel import CheckerSequentialChannelMixin
from apps.udi.manager import UDIManager


@pytest.mark.parametrize("props,verified,ids", [
    ({}, True, None), (None, True, None),
    ({"allowed_m3u_profile_ids": None}, True, None),
    ({"allowed_m3u_profile_ids": []}, True, frozenset()),
    ({"allowed_m3u_profile_ids": [3, 3]}, True, frozenset({3})),
    ({"allowed_m3u_profile_ids": [True]}, False, None),
    ({"allowed_m3u_profile_ids": ["3"]}, False, None),
    ({"allowed_m3u_profile_ids": [0]}, False, None),
    ({"allowed_m3u_profile_ids": "all"}, False, None),
    ([], False, None),
])
def test_dispatcharr_allowlist_semantics(props, verified, ids):
    access = ProfileAccess.from_user({"custom_properties": props})
    assert access.verified is verified
    assert access.allowed_ids == ids
    if not verified:
        assert not access.allows(3)


def cache_fixture(monkeypatch):
    cache = ProfileAccessCache()
    clock = [100.0]
    identity = ["user-one"]
    monkeypatch.setattr("apps.core.provider_profile_access.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(cache, "_connection", lambda: ("http://dispatcharr.test", {"X-API-Key": "test"}, identity[0]))
    response = Mock()
    response.json.return_value = {"id": 10, "user_level": 10,
                                  "custom_properties": {"allowed_m3u_profile_ids": [3]}}
    get = Mock(return_value=response)
    monkeypatch.setattr("apps.core.provider_profile_access.requests.get", get)
    return cache, clock, identity, response, get


def test_expiry_and_failed_refresh_never_restore_unrestricted_access(monkeypatch):
    cache, clock, _, _, get = cache_fixture(monkeypatch)
    assert cache.get().allows(3)
    assert cache.get().allows(3)
    assert get.call_count == 1
    clock[0] += cache.ttl + 1
    get.side_effect = requests.ConnectionError("unavailable")
    assert not cache.get().verified
    assert not cache.get().allows(3)
    assert get.call_count == 2


def test_new_credentials_do_not_inherit_old_permissions(monkeypatch):
    cache, _, identity, response, get = cache_fixture(monkeypatch)
    assert cache.get().allows(3)
    identity[0] = "user-two"
    response.json.return_value["custom_properties"] = {"allowed_m3u_profile_ids": []}
    assert not cache.get().allows(3)
    assert get.call_count == 2


def test_connection_edit_during_fetch_rejects_old_user(monkeypatch):
    cache, _, identity, _, get = cache_fixture(monkeypatch)
    def changed():
        identity[0] = "user-two"
        return {"user_level": 10, "custom_properties": {}}
    get.return_value.json.side_effect = changed
    assert not cache.get().verified


@pytest.mark.parametrize("level", [0, 1, None, True])
def test_unverified_admin_role_does_not_authorize_profiles(monkeypatch, level):
    cache, _, _, response, _ = cache_fixture(monkeypatch)
    response.json.return_value = {"user_level": level}
    assert not cache.get().verified


class RestrictedUDI:
    supports_provider_profile_access = True
    supports_account_authority_lease = True

    def __init__(self):
        self.access = ProfileAccess(True, frozenset({3}), "fixture-user")
        self.profiles = [
            {"id": 1, "name": "Residential", "is_active": True, "is_default": True, "max_streams": 1},
            {"id": 2, "name": "Residential alternate", "is_active": True, "max_streams": 1,
             "search_pattern": r"^https://home\.example\.test/(.*)$", "replace_pattern": "https://other.example.test/$1"},
            {"id": 3, "name": "VPS", "is_active": True, "max_streams": 1,
             "search_pattern": r"^https://home\.example\.test/(.*)$", "replace_pattern": "https://vps.example.test/$1"},
        ]
        self.account = {"id": 1, "max_streams": 1, "profiles": self.profiles}
        self.usage = {}
        self.stream = {"id": 100, "name": "Fixture", "m3u_account_id": 1,
                       "url": "https://home.example.test/live/test/password/1.ts"}

    def get_provider_profile_access(self, force=False):
        return self.access

    def get_m3u_account_by_id(self, account_id):
        return self.account if account_id == 1 else None

    def get_active_stream_context_per_profile(self, account_id):
        return self.usage

    def get_stream_by_id(self, stream_id):
        return self.stream

    def resolve_profile_stream_url(self, stream, profile):
        return UDIManager.resolve_profile_stream_url(self, stream, profile)

    @contextmanager
    def account_authority_lease(self, account_id):
        yield self.access.project_account(self.account)


def fixture_limiter(monkeypatch, udi=None):
    udi = udi or RestrictedUDI()
    limiter = limits.AccountStreamLimiter(udi)
    monkeypatch.setattr(limits, "get_account_limiter", lambda: limiter)
    assert limits.initialize_account_limits([udi.account])
    return udi, limiter


def test_only_allowed_route_is_reserved_and_reported(monkeypatch):
    udi, limiter = fixture_limiter(monkeypatch)
    assert limiter.get_account_limit(1) == 1
    assert [s["id"] for s in limiter.get_profile_slot_snapshot(1)] == [3]
    ok, _, profile, url = limiter.reserve_profile_for_stream_with_url(udi.stream)
    assert ok and profile["id"] == 3
    assert url.startswith("https://vps.example.test/")
    assert all(p["is_active"] for p in udi.profiles)  # raw inventory unchanged
    limiter.release_profile(profile)


@pytest.mark.parametrize("access,reason", [
    (ProfileAccess(True, frozenset()), "provider_profile_not_allowed"),
    (ProfileAccess(), "provider_permissions_unverified"),
])
def test_no_allowed_profile_never_falls_back_or_probes(monkeypatch, access, reason):
    udi = RestrictedUDI()
    udi.access = access
    udi, limiter = fixture_limiter(monkeypatch, udi)
    probe = Mock()
    result = limits.SmartStreamScheduler(limiter, global_limit=1).check_streams_with_limits(
        [udi.stream], probe, stagger_delay=0, provider_wait_timeout=0,
    )[0]
    probe.assert_not_called()
    assert result["provider_limit_skipped"]
    assert result["skipped_reason"] == reason
    assert limiter.get_available_slots(1) == 0
    assert limiter.reserve_profile_for_stream_with_url(udi.stream) == (False, reason, None, "")


def test_profileless_restricted_provider_has_no_raw_url_fallback(monkeypatch):
    udi = RestrictedUDI()
    udi.account["profiles"] = []
    udi, limiter = fixture_limiter(monkeypatch, udi)
    assert limiter.reserve_profile_for_stream_with_url(udi.stream)[0] is False
    udi.access = ProfileAccess(True)
    assert limits.initialize_account_limits([udi.account])
    assert limiter.reserve_profile_for_stream_with_url(udi.stream)[3] == udi.stream["url"]


def test_revoked_rights_invalidate_existing_reservation(monkeypatch):
    udi, limiter = fixture_limiter(monkeypatch)
    ok, _, profile, _ = limiter.reserve_profile_for_stream_with_url(udi.stream)
    assert ok
    udi.access = ProfileAccess(True, frozenset(), "fixture-user")
    assert limiter.should_preempt_profile_for_viewer(profile, account_id=1)
    assert not limiter.reserve_profile_for_stream_with_url(udi.stream)[0]
    limiter.release_profile(profile)


def test_residential_viewers_do_not_consume_distinct_vps_capacity(monkeypatch):
    udi, limiter = fixture_limiter(monkeypatch)
    udi.usage = {1: {"active_streams": 1, "real_viewers": 1, "shadow_watchers": 0}}
    assert limiter.get_available_slots(1) == 1
    assert limiter.acquire(1, timeout=0)[0]
    limiter.release(1)


def test_forbidden_alias_viewer_still_consumes_allowed_route(monkeypatch):
    udi = RestrictedUDI()
    alias = dict(udi.profiles[2], id=4)
    udi.profiles.append(alias)
    udi.usage = {4: {"active_streams": 1, "real_viewers": 1, "shadow_watchers": 0}}
    udi, limiter = fixture_limiter(monkeypatch, udi)
    assert limiter.get_available_slots(1) == 0
    assert not limiter.reserve_profile_for_stream_with_url(udi.stream)[0]


def test_rights_change_during_url_resolution_cannot_commit(monkeypatch):
    udi, limiter = fixture_limiter(monkeypatch)
    original = udi.resolve_profile_stream_url
    def revoke(stream, profile):
        result = original(stream, profile)
        udi.access = ProfileAccess(True, frozenset(), "fixture-user")
        return result
    udi.resolve_profile_stream_url = revoke
    assert not limiter.reserve_profile_for_stream_with_url(udi.stream)[0]


def test_sequential_uses_shared_capacity_path_including_rechecks():
    class Service(CheckerSequentialChannelMixin):
        _run_capacity_limited_stream_probes = Mock(return_value=[{"status": "ok"}])
        _checker_analyze_stream = Mock(side_effect=AssertionError("unreserved probe"))
    service = Service()
    udi = RestrictedUDI()
    for retry in (True, False):
        assert service._probe_sequential_stream(stream=udi.stream, udi=udi,
            stream_url=udi.stream["url"], stream_id=100, stream_name="Fixture",
            retry_missing_bitrate=retry) == {"status": "ok"}
    assert service._run_capacity_limited_stream_probes.call_count == 2


def test_direct_transformation_cannot_bypass_explicit_deny():
    class DirectUDI(RestrictedUDI):
        _get_stream_m3u_account_id = staticmethod(UDIManager._get_stream_m3u_account_id)
        find_available_profile_for_stream = UDIManager.find_available_profile_for_stream
        apply_profile_url_transformation = UDIManager.apply_profile_url_transformation
        _ensure_initialized = lambda self: None
    udi = DirectUDI()
    udi.access = ProfileAccess(True, frozenset())
    assert udi.apply_profile_url_transformation(udi.stream) == ""
    assert udi.apply_profile_url_transformation(udi.stream, udi.profiles[0]) == ""


def test_projection_can_be_refreshed_without_losing_original_active_state():
    udi = RestrictedUDI()
    projected = checking_accounts(udi, [udi.account])
    udi.access = ProfileAccess(True)
    assert all(p["is_active"] for p in checking_accounts(udi, projected)[0]["profiles"])
