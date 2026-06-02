import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import patch
try:
    from backend.api_utils import fetch_data_from_url, _get_base_url
except ModuleNotFoundError:
    from apps.core.api_utils import fetch_data_from_url, _get_base_url

# List of Dispatcharr API endpoints used in the project
API_ENDPOINTS = [
    "/api/channels/channels/",
    "/api/channels/channels/{channel_id}/streams/"
]

def test_fetch_channels(monkeypatch):
    base_url = "http://100.107.251.48:9191"
    monkeypatch.setenv("DISPATCHARR_BASE_URL", base_url)
    monkeypatch.setenv("DISPATCHARR_TOKEN", "test-token")
    with patch("backend.api_utils.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"results": []}
        result = fetch_data_from_url(f"{base_url}/api/channels/channels/")
        assert result == {"results": []}
        mock_get.assert_called_once()

def test_fetch_channel_streams(monkeypatch):
    base_url = "http://mockserver"
    channel_id = 123
    monkeypatch.setenv("DISPATCHARR_BASE_URL", base_url)
    monkeypatch.setenv("DISPATCHARR_TOKEN", "test-token")
    with patch("backend.api_utils.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = []
        result = fetch_data_from_url(f"{base_url}/api/channels/channels/{channel_id}/streams/")
        assert result == []
        mock_get.assert_called_once()
