"""Client for AceStream legacy monitoring sessions exposed by orchestrator."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, Optional, Set
from urllib.parse import parse_qs, urlparse

import requests

from apps.config.acestream_orchestrator_config import get_acestream_orchestrator_config


CONTENT_ID_RE = re.compile(r"([0-9a-fA-F]{40})")


def normalize_content_id(value: Optional[str]) -> Optional[str]:
    """Normalize content IDs for case-insensitive matching."""
    if not value or not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if text.lower().startswith("acestream://"):
        text = text[len("acestream://") :]

    # Handle URLs like /ace/getstream?id=<40hex>
    try:
        parsed = urlparse(text)
        query_id = parse_qs(parsed.query).get("id", [None])[0]
        if isinstance(query_id, str):
            qmatch = CONTENT_ID_RE.search(query_id)
            if qmatch:
                return qmatch.group(1).lower()
    except Exception:
        pass

    match = CONTENT_ID_RE.search(text)
    if not match:
        return None

    return match.group(1).lower()


def _extract_started_content_ids(payload: Any) -> Set[str]:
    """Extract normalized content IDs from the optional /streams response shape."""
    ids: Set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = normalize_content_id(str(key))
            if normalized_key:
                ids.add(normalized_key)

            if isinstance(value, dict):
                for candidate_key in ("content_id", "id", "stream_url", "url"):
                    normalized_value = normalize_content_id(value.get(candidate_key))
                    if normalized_value:
                        ids.add(normalized_value)
            elif isinstance(value, str):
                normalized_value = normalize_content_id(value)
                if normalized_value:
                    ids.add(normalized_value)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                normalized = normalize_content_id(item)
                if normalized:
                    ids.add(normalized)
            elif isinstance(item, dict):
                for candidate_key in ("content_id", "id", "stream_url", "url"):
                    normalized = normalize_content_id(item.get(candidate_key))
                    if normalized:
                        ids.add(normalized)

    return ids


class AceStreamMonitoringClient:
    """Thin HTTP client around orchestrator monitoring endpoints."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout_s: float = 10.0):
        config = get_acestream_orchestrator_config()
        resolved_base_url = base_url or config.get_base_url() or os.getenv("ACESTREAM_ORCHESTRATOR_BASE_URL") or os.getenv("ORCHESTRATOR_BASE_URL") or ""
        resolved_api_key = api_key or config.get_api_key() or os.getenv("ACESTREAM_ORCHESTRATOR_API_KEY") or os.getenv("ORCHESTRATOR_API_KEY") or ""

        self.base_url = str(resolved_base_url).strip().rstrip("/")
        self.api_key = str(resolved_api_key).strip()
        self.timeout_s = timeout_s

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        response = requests.request(
            method=method,
            url=url,
            headers=self._headers(),
            timeout=self.timeout_s,
            **kwargs,
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return None

    def start_session(self, payload: Dict[str, Any]) -> Any:
        return self._request("POST", "/api/v1/ace/monitor/legacy/start", json=payload)

    def list_sessions(self) -> Dict[str, Any]:
        data = self._request("GET", "/api/v1/ace/monitor/legacy", params={"include_recent_status": "false"})
        return data if isinstance(data, dict) else {"items": []}

    def get_session(self, monitor_id: str) -> Dict[str, Any]:
        data = self._request("GET", f"/api/v1/ace/monitor/legacy/{monitor_id}", params={"include_recent_status": "false"})
        return data if isinstance(data, dict) else {}

    def stop_session(self, monitor_id: str) -> Any:
        return self._request("DELETE", f"/api/v1/ace/monitor/legacy/{monitor_id}")

    def delete_entry(self, monitor_id: str) -> Any:
        return self._request("DELETE", f"/api/v1/ace/monitor/legacy/{monitor_id}/entry")

    def parse_m3u(self, m3u_content: str) -> Any:
        return self._request("POST", "/api/v1/ace/monitor/legacy/parse-m3u", json={"m3u_content": m3u_content})

    def list_started_streams(self) -> Any:
        response = requests.get(
            f"{self.base_url}/api/v1/streams",
            headers=self._headers(),
            params={"status": "started"},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return response.json()

    def annotate_with_playback(self, session_data: Dict[str, Any], started_streams_payload: Any) -> Dict[str, Any]:
        started_ids = _extract_started_content_ids(started_streams_payload)
        content_id = normalize_content_id(session_data.get("content_id"))
        session_data["currently_played"] = bool(content_id and content_id in started_ids)
        return session_data

    def annotate_many_with_playback(self, sessions: Iterable[Dict[str, Any]], started_streams_payload: Any) -> list:
        started_ids = _extract_started_content_ids(started_streams_payload)
        enriched = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            content_id = normalize_content_id(item.get("content_id"))
            copy_item = dict(item)
            copy_item["currently_played"] = bool(content_id and content_id in started_ids)
            enriched.append(copy_item)
        return enriched
