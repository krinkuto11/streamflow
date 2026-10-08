"""Connectivity responsibilities for the shared StreamCheckerService instance."""

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional
from apps.core.api_utils import _get_auth_headers
from apps.stream.connectivity_guard import ConnectivityCheckResult
from apps.core.auth import _refresh_token

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerConnectivityMixin:
    def _run_connectivity_guard(
        self,
        phase: str,
        *,
        operation: str = 'destructive_write',
        channel_id: Optional[int] = None,
        channel_name: Optional[str] = None,
    ) -> ConnectivityCheckResult:
        """Run and record the fail-closed connectivity guard."""
        try:
            result = self.connectivity_guard.check(
                config=self.config.get('connectivity_guard', {}),
                dispatcharr_base_url=self._checker_get_base_url(),
                dispatcharr_headers_provider=_get_auth_headers,
                dispatcharr_auth_refresh_provider=_refresh_token,
                operation=operation,
            )
        except Exception as exc:
            logger.warning("Connectivity guard failed unexpectedly during %s: %s", phase, exc)
            result = ConnectivityCheckResult(
                ok=False,
                reason='connectivity_guard_error',
                message='Connectivity could not be verified',
                details={'phase': phase},
            )

        status = result.to_dict()
        status['phase'] = phase
        status['operation'] = operation
        if channel_id is not None:
            status['channel_id'] = channel_id
        if channel_name:
            status['channel_name'] = channel_name
        status['checked_at'] = datetime.now().isoformat()
        self.connectivity_guard_status = status
        return result


    def _maybe_refresh_stale_connectivity_guard(self, stream_checking_mode: bool) -> None:
        """Recheck old idle guard failures so recovered systems stop showing stale errors."""
        if stream_checking_mode:
            return

        current_status = self.connectivity_guard_status or {}
        if current_status.get('ok') is not False:
            return

        config = self.config.get('connectivity_guard', {}) or {}
        if config.get('enabled', True) is False:
            return

        checked_at = current_status.get('checked_at')
        if not checked_at:
            return

        try:
            last_checked = datetime.fromisoformat(checked_at)
        except (TypeError, ValueError):
            return

        interval = self._bounded_float(
            config.get('stale_recheck_interval_seconds', 60),
            default=60.0,
            minimum=10.0,
            maximum=3600.0,
        )
        if (datetime.now() - last_checked).total_seconds() < interval:
            return

        now = time.time()
        if now - self._last_connectivity_guard_recovery_probe_at < interval:
            return

        self._last_connectivity_guard_recovery_probe_at = now
        logger.info("Rechecking stale connectivity guard failure after %.0fs", interval)
        self._run_connectivity_guard(
            'stale_failure_recovery',
            operation='analysis',
        )


    @staticmethod
    def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = default
        return max(minimum, min(maximum, numeric))


    def _connectivity_abort_payload(
        self,
        result: ConnectivityCheckResult,
        *,
        channel_id: Optional[int] = None,
        channel_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            'success': False,
            'error': 'connectivity_guard',
            'aborted': True,
            'skip_reason': 'connectivity_guard',
            'message': result.message,
            'connectivity_guard': result.to_dict(),
        }
        if channel_id is not None:
            payload['channel_id'] = channel_id
        if channel_name is not None:
            payload['channel_name'] = channel_name
        return payload


    def _fail_channel_for_connectivity(
        self,
        result: ConnectivityCheckResult,
        *,
        channel_id: int,
        channel_name: Optional[str] = None,
        queue_entry_token: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.check_queue.mark_failed(
            channel_id,
            result.message,
            entry_token=queue_entry_token,
        )
        return self._connectivity_abort_payload(
            result,
            channel_id=channel_id,
            channel_name=channel_name,
        )


    def _require_quality_check_connectivity(
        self,
        *,
        phase: str,
        channel_id: Optional[int] = None,
        channel_name: Optional[str] = None,
        update_progress: bool = True,
        progress_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ConnectivityCheckResult]:
        """Return a failed result when the requested quality operation must abort."""
        destructive_phases = {
            'mark_dead_stream',
            'keep_dead_stream_marked',
            'channel_stream_update',
            'single_channel_validation_removal',
            'single_channel_matching_update',
            'automation_quality_preflight',
        }
        operation = (
            'destructive_write'
            if phase in destructive_phases
            else 'analysis'
        )
        result = self._run_connectivity_guard(
            phase,
            operation=operation,
            channel_id=channel_id,
            channel_name=channel_name,
        )
        if result.ok:
            return None
        progress_context = dict(progress_context or {})

        recoverable_phases = destructive_phases
        config = self.config.get('connectivity_guard', {}) or {}
        recovery_wait_seconds = self._bounded_float(
            config.get('recovery_wait_seconds', 240),
            default=240.0,
            minimum=0.0,
            maximum=600.0,
        )
        recovery_poll_seconds = self._bounded_float(
            config.get('recovery_poll_seconds', 10),
            default=10.0,
            minimum=1.0,
            maximum=120.0,
        )
        if phase in recoverable_phases and recovery_wait_seconds > 0:
            safe_channel_name = channel_name or (
                f"Channel {channel_id}" if channel_id is not None else "Quality check"
            )
            deadline = time.monotonic() + recovery_wait_seconds
            first_failure = {
                'reason': result.reason,
                'message': result.message,
                'checked_at': datetime.now().isoformat(),
            }
            recovery_attempts = 0
            logger.warning(
                "Connectivity guard failed at %s for %s: %s; waiting up to %.0fs for recovery",
                phase,
                safe_channel_name,
                result.message,
                recovery_wait_seconds,
            )
            while time.monotonic() < deadline and not self.abort_current_check.is_set():
                remaining = max(0.0, deadline - time.monotonic())
                sleep_for = min(recovery_poll_seconds, remaining)
                recovery_attempts += 1
                self.connectivity_guard_status = {
                    **dict(self.connectivity_guard_status or {}),
                    'recovery': {
                        'active': True,
                        'first_failure': first_failure,
                        'attempts': recovery_attempts,
                        'remaining_seconds': round(remaining, 1),
                        'channel_id': channel_id,
                        'channel_name': safe_channel_name,
                    },
                }
                if update_progress and channel_id is not None:
                    try:
                        self.progress.update(
                            channel_id=channel_id,
                            channel_name=safe_channel_name,
                            current=0,
                            total=0,
                            status='waiting_connectivity',
                            step='Waiting for Dispatcharr API',
                            step_detail=(
                                f"{result.message}; retrying for up to {int(remaining)}s"
                            ),
                            **progress_context,
                        )
                    except Exception as exc:
                        logger.debug("Failed to publish connectivity recovery progress: %s", exc)
                if sleep_for > 0:
                    time.sleep(sleep_for)
                recovery_result = self._run_connectivity_guard(
                    f"{phase}_recovery",
                    operation=operation,
                    channel_id=channel_id,
                    channel_name=safe_channel_name,
                )
                if recovery_result.ok:
                    self.connectivity_guard_status['recovery'] = {
                        'active': False,
                        'first_failure': first_failure,
                        'attempts': recovery_attempts,
                        'remaining_seconds': round(max(0.0, deadline - time.monotonic()), 1),
                        'channel_id': channel_id,
                        'channel_name': safe_channel_name,
                        'recovered': True,
                    }
                    logger.info(
                        "Connectivity guard recovered at %s for %s; continuing quality work",
                        phase,
                        safe_channel_name,
                    )
                    return None
                result = recovery_result
            self.connectivity_guard_status['recovery'] = {
                'active': False,
                'first_failure': first_failure,
                'attempts': recovery_attempts,
                'remaining_seconds': 0.0,
                'channel_id': channel_id,
                'channel_name': safe_channel_name,
                'recovered': False,
                'exhausted': not self.abort_current_check.is_set(),
            }

        self.abort_current_check.set()
        self._cancel_queueing = True
        safe_channel_name = channel_name or (f"Channel {channel_id}" if channel_id is not None else "Quality check")
        logger.error("Aborting quality check at %s: %s", phase, result.message)

        if update_progress and channel_id is not None:
            try:
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=safe_channel_name,
                    current=0,
                    total=0,
                    status='aborted',
                    step='Connectivity check failed',
                    step_detail=result.message,
                    **progress_context,
                )
            except Exception as exc:
                logger.debug("Failed to publish connectivity abort progress: %s", exc)

        return result

