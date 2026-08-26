from unittest.mock import MagicMock, Mock, patch

from apps.core.api_utils import update_channel_streams
from apps.stream.stream_checker_service import StreamCheckerService


def test_merge_protected_stream_order_keeps_active_stream_slot():
    result = StreamCheckerService._merge_protected_stream_order(
        original_stream_ids=[101, 102, 103, 104],
        reordered_ids=[104, 103, 101],
        protected_stream_ids={102},
    )

    assert result == [104, 102, 103, 101]


def test_active_viewer_skipped_streams_carry_reason():
    service = StreamCheckerService.__new__(StreamCheckerService)

    skipped = service._active_viewer_skipped_streams(
        [{"id": "202", "name": "Watched Stream"}],
        {202},
    )

    assert skipped == [{
        "id": 202,
        "stream_id": 202,
        "name": "Watched Stream",
        "stream_name": "Watched Stream",
        "skip_reason": "active_viewer_protected",
        "reason_detail": "active_viewer_protected",
        "status": "active_viewer_protected",
    }]


@patch("apps.stream.dead_streams_tracker.DeadStreamsTracker")
@patch("api_utils.patch_request")
@patch("api_utils.get_udi_manager")
def test_update_channel_streams_keeps_protected_dead_stream(mock_get_udi, mock_patch, mock_tracker_cls):
    mock_udi = MagicMock()
    mock_udi.get_valid_stream_ids.return_value = {1, 2, 3}
    mock_udi.get_streams.return_value = [
        {"id": 1, "url": "http://example.test/live.m3u8"},
        {"id": 2, "url": "http://example.test/protected-dead.m3u8"},
        {"id": 3, "url": "http://example.test/dead.m3u8"},
    ]
    mock_get_udi.return_value = mock_udi

    mock_tracker = MagicMock()
    mock_tracker.is_offline.side_effect = lambda url: "dead" in url
    mock_tracker_cls.return_value = mock_tracker

    mock_response = Mock()
    mock_response.status_code = 200
    mock_patch.return_value = mock_response

    result = update_channel_streams(
        77,
        [1, 2, 3],
        protected_stream_ids={2},
    )

    assert result is True
    data = mock_patch.call_args[0][1]
    assert data["streams"] == [1, 2]
