"""Connectivity preflight checks for destructive stream quality operations."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlparse, urlunparse

import requests

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


DEFAULT_INTERNET_PROBE_URLS = (
    "https://www.google.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
)


@dataclass
class ConnectivityCheckResult:
    """Sanitized result for a connectivity guard check."""

    ok: bool
    reason: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
            "details": self.details,
        }


class StreamConnectivityGuard:
    """Proves network/API reachability before destructive quality checks.

    The checker intentionally fails closed. A result is considered healthy only
    when a public internet probe and the configured Dispatcharr API probe both
    complete successfully. Details contain only probe labels, hosts, and HTTP
    status codes so status payloads and logs can be shared safely.
    """

    def __init__(
        self,
        *,
        requests_module=requests,
        socket_module=socket,
        default_internet_probe_urls: Iterable[str] = DEFAULT_INTERNET_PROBE_URLS,
    ) -> None:
        self._requests = requests_module
        self._socket = socket_module
        self._default_internet_probe_urls = tuple(default_internet_probe_urls)

    def check(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        dispatcharr_base_url: Optional[str],
        dispatcharr_headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
        dispatcharr_auth_refresh_provider: Optional[Callable[[], bool]] = None,
    ) -> ConnectivityCheckResult:
        cfg = config or {}
        if cfg.get("enabled", True) is False:
            return ConnectivityCheckResult(
                ok=True,
                reason="disabled",
                message="Connectivity guard is disabled",
                details={"enabled": False},
            )

        timeout_seconds = max(0.1, self._safe_float(cfg.get("timeout_seconds", 3.0), 3.0))
        retry_attempts = max(0, min(10, self._safe_int(cfg.get("retry_attempts", 2), 2)))
        retry_backoff_seconds = max(
            0.0,
            min(30.0, self._safe_float(cfg.get("retry_backoff_seconds", 1.0), 1.0)),
        )
        require_internet = cfg.get("require_internet", True) is not False
        require_dispatcharr_api = cfg.get("require_dispatcharr_api", True) is not False
        checked: Dict[str, Any] = {}

        if require_internet:
            internet_urls = cfg.get("internet_probe_urls") or cfg.get("internet_probe_url")
            if isinstance(internet_urls, str):
                internet_urls = [internet_urls]
            if not internet_urls:
                internet_urls = self._default_internet_probe_urls

            internet_result = self._probe_any_http_endpoint(
                urls=internet_urls,
                label="internet",
                timeout_seconds=timeout_seconds,
                retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
            )
            checked["internet"] = internet_result.details
            if not internet_result.ok:
                return internet_result

        if require_dispatcharr_api:
            if not dispatcharr_base_url:
                return ConnectivityCheckResult(
                    ok=False,
                    reason="dispatcharr_base_url_missing",
                    message="Dispatcharr API connectivity could not be verified because no base URL is configured",
                    details={"dispatcharr_api": {"configured": False}},
                )

            headers: Dict[str, str] = {}
            if dispatcharr_headers_provider is not None:
                try:
                    headers = dispatcharr_headers_provider()
                except Exception as exc:
                    logger.warning("Connectivity guard could not prepare Dispatcharr auth headers: %s", exc)
                    return ConnectivityCheckResult(
                        ok=False,
                        reason="dispatcharr_auth_unavailable",
                        message="Dispatcharr API connectivity could not be verified because authentication is unavailable",
                        details={"dispatcharr_api": {"host": self._safe_host(dispatcharr_base_url)}},
                    )

            dispatcharr_probe_url = self._build_dispatcharr_probe_url(dispatcharr_base_url)
            dispatcharr_result = self._probe_http_endpoint(
                url=dispatcharr_probe_url,
                label="dispatcharr_api",
                timeout_seconds=timeout_seconds,
                retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                headers=headers,
                accept_unauthorized=False,
            )
            if (
                dispatcharr_result.reason == "dispatcharr_auth_failed"
                and dispatcharr_headers_provider is not None
                and dispatcharr_auth_refresh_provider is not None
            ):
                dispatcharr_result = self._retry_dispatcharr_probe_after_auth_refresh(
                    dispatcharr_base_url=dispatcharr_base_url,
                    dispatcharr_probe_url=dispatcharr_probe_url,
                    dispatcharr_headers_provider=dispatcharr_headers_provider,
                    dispatcharr_auth_refresh_provider=dispatcharr_auth_refresh_provider,
                    timeout_seconds=timeout_seconds,
                    retry_attempts=retry_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                    previous_result=dispatcharr_result,
                )
            checked["dispatcharr_api"] = dispatcharr_result.details
            if not dispatcharr_result.ok:
                return dispatcharr_result

        return ConnectivityCheckResult(
            ok=True,
            reason="ok",
            message="Connectivity verified",
            details=checked,
        )

    def _retry_dispatcharr_probe_after_auth_refresh(
        self,
        *,
        dispatcharr_base_url: str,
        dispatcharr_probe_url: str,
        dispatcharr_headers_provider: Callable[[], Dict[str, str]],
        dispatcharr_auth_refresh_provider: Callable[[], bool],
        timeout_seconds: float,
        retry_attempts: int,
        retry_backoff_seconds: float,
        previous_result: ConnectivityCheckResult,
    ) -> ConnectivityCheckResult:
        try:
            refreshed = dispatcharr_auth_refresh_provider()
        except Exception as exc:
            logger.warning("Connectivity guard could not refresh Dispatcharr auth: %s", exc)
            previous_result.details = {
                **previous_result.details,
                "auth_refresh_attempted": True,
                "auth_refresh_ok": False,
            }
            return previous_result

        if not refreshed:
            previous_result.details = {
                **previous_result.details,
                "auth_refresh_attempted": True,
                "auth_refresh_ok": False,
            }
            return previous_result

        try:
            refreshed_headers = dispatcharr_headers_provider()
        except Exception as exc:
            logger.warning("Connectivity guard could not prepare refreshed Dispatcharr auth headers: %s", exc)
            return ConnectivityCheckResult(
                ok=False,
                reason="dispatcharr_auth_unavailable",
                message="Dispatcharr API connectivity could not be verified because authentication is unavailable",
                details={
                    "dispatcharr_api": {
                        "host": self._safe_host(dispatcharr_base_url),
                        "auth_refresh_attempted": True,
                        "auth_refresh_ok": True,
                    }
                },
            )

        retry_result = self._probe_http_endpoint(
            url=dispatcharr_probe_url,
            label="dispatcharr_api",
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            headers=refreshed_headers,
            accept_unauthorized=False,
        )
        retry_result.details = {
            **retry_result.details,
            "auth_refresh_attempted": True,
            "auth_refresh_ok": True,
        }
        return retry_result

    def _probe_any_http_endpoint(
        self,
        *,
        urls: Iterable[str],
        label: str,
        timeout_seconds: float,
        retry_attempts: int,
        retry_backoff_seconds: float,
    ) -> ConnectivityCheckResult:
        last_result: Optional[ConnectivityCheckResult] = None
        attempts = []

        for url in urls:
            result = self._probe_http_endpoint(
                url=url,
                label=label,
                timeout_seconds=timeout_seconds,
                retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                accept_unauthorized=True,
            )
            attempts.append(result.details)
            if result.ok:
                result.details = {"attempts": attempts}
                return result
            last_result = result

        if last_result is None:
            return ConnectivityCheckResult(
                ok=False,
                reason="internet_probe_missing",
                message="Internet connectivity could not be verified because no probe endpoint is configured",
                details={label: {"configured": False}},
            )

        return ConnectivityCheckResult(
            ok=False,
            reason=last_result.reason,
            message=last_result.message,
            details={"attempts": attempts},
        )

    def _probe_http_endpoint(
        self,
        *,
        url: str,
        label: str,
        timeout_seconds: float,
        retry_attempts: int,
        retry_backoff_seconds: float,
        headers: Optional[Dict[str, str]] = None,
        accept_unauthorized: bool,
    ) -> ConnectivityCheckResult:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        safe_details: Dict[str, Any] = {"label": label, "host": host}

        if parsed.scheme not in {"http", "https"} or not host:
            return ConnectivityCheckResult(
                ok=False,
                reason="invalid_probe_endpoint",
                message=f"{label} connectivity probe is not a valid HTTP endpoint",
                details=safe_details,
            )

        try:
            self._socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return ConnectivityCheckResult(
                ok=False,
                reason="dns_resolution_failed",
                message=f"DNS resolution failed for {label} connectivity probe",
                details=safe_details,
            )
        except OSError:
            return ConnectivityCheckResult(
                ok=False,
                reason="network_unreachable",
                message=f"Network resolution failed for {label} connectivity probe",
                details=safe_details,
            )

        max_attempts = retry_attempts + 1
        last_result: Optional[ConnectivityCheckResult] = None

        for attempt in range(1, max_attempts + 1):
            attempt_details = {
                **safe_details,
                "attempts": attempt,
                "max_attempts": max_attempts,
            }
            try:
                response = self._requests.get(
                    url,
                    headers=headers or {},
                    timeout=timeout_seconds,
                    allow_redirects=True,
                )
            except requests.exceptions.Timeout:
                last_result = ConnectivityCheckResult(
                    ok=False,
                    reason="connectivity_timeout",
                    message=f"{label} connectivity probe timed out after {attempt} attempt(s)",
                    details=attempt_details,
                )
            except requests.exceptions.RequestException:
                last_result = ConnectivityCheckResult(
                    ok=False,
                    reason="network_unreachable",
                    message=f"{label} connectivity probe could not reach its endpoint after {attempt} attempt(s)",
                    details=attempt_details,
                )
            else:
                attempt_details["status_code"] = response.status_code

                if 200 <= response.status_code < 400:
                    return ConnectivityCheckResult(True, "ok", f"{label} connectivity verified", attempt_details)
                if accept_unauthorized and response.status_code in {401, 403}:
                    return ConnectivityCheckResult(True, "ok", f"{label} connectivity verified", attempt_details)

                if response.status_code in {401, 403}:
                    return ConnectivityCheckResult(
                        ok=False,
                        reason="dispatcharr_auth_failed",
                        message="Dispatcharr API connectivity could not be verified because authentication failed",
                        details=attempt_details,
                    )

                last_result = ConnectivityCheckResult(
                    ok=False,
                    reason="endpoint_unhealthy",
                    message=f"{label} connectivity probe returned HTTP {response.status_code} after {attempt} attempt(s)",
                    details=attempt_details,
                )
                if not self._should_retry_status(response.status_code):
                    return last_result

            if attempt < max_attempts and retry_backoff_seconds > 0:
                time.sleep(retry_backoff_seconds)

        return last_result or ConnectivityCheckResult(
            ok=False,
            reason="network_unreachable",
            message=f"{label} connectivity probe could not reach its endpoint",
            details={**safe_details, "attempts": max_attempts, "max_attempts": max_attempts},
        )

    @staticmethod
    def _build_dispatcharr_probe_url(base_url: str) -> str:
        clean = base_url.rstrip("/")
        return f"{clean}/api/channels/channels/"

    @staticmethod
    def _safe_host(url: str) -> Optional[str]:
        return urlparse(url).hostname

    @staticmethod
    def _should_retry_status(status_code: int) -> bool:
        return status_code in {408, 425, 429} or status_code >= 500

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default


def sanitize_probe_url(url: str) -> str:
    """Return scheme/host/port/path only; query and credentials are dropped."""

    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))
