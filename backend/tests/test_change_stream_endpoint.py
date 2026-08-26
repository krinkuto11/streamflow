from unittest.mock import Mock, patch

from apps.core import api_utils


def test_change_channel_stream_uses_dispatcharr_proxy_route():
    response = Mock(status_code=200)

    with (
        patch.object(api_utils, "_get_base_url", return_value="http://dispatcharr.local"),
        patch.object(api_utils, "post_request", return_value=response) as post_request,
    ):
        assert api_utils.change_channel_stream("uuid-1", stream_id=123) is True

    post_request.assert_called_once_with(
        "http://dispatcharr.local/proxy/ts/change_stream/uuid-1",
        {"stream_id": 123},
    )
