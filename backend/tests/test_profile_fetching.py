import pytest

from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.udi.fetcher import UDIFetcher


pytestmark = pytest.mark.live


def test_profile_fetching_from_configured_dispatcharr():
    """Fetch and validate real profile rows when live credentials are supplied."""
    config = get_dispatcharr_config()
    if not config.is_configured():
        pytest.skip("live Dispatcharr configuration is not available")

    profiles = UDIFetcher().fetch_channel_profiles()

    assert isinstance(profiles, list)
    for profile in profiles:
        assert isinstance(profile, dict)
        assert profile.get("id") is not None
        assert isinstance(profile.get("channels", []), list)
