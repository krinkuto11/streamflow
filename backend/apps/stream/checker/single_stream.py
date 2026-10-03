"""Single stream responsibilities for the shared StreamCheckerService instance."""

import logging
from typing import Any, Dict
from apps.core.log_sanitizer import audit_ref as _audit_ref

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerSingleStreamMixin:
    def check_single_stream(
        self,
        stream_id: int,
        *,
        persist: bool = True,
        blank_check_enabled: bool = False,
        freeze_check_enabled: bool = False,
        loop_check_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Reserve and run one synchronous, capacity-limited stream probe."""
        if not self._begin_single_stream_check_operation():
            return {
                'success': False,
                'error': 'stream_checker_active',
                'message': 'Stream Checker work is already active',
                'stream_id': stream_id,
                'run_mode': 'single_stream_check',
                'persisted': False,
            }
        try:
            return self._check_single_stream_reserved(
                stream_id,
                persist=persist,
                blank_check_enabled=blank_check_enabled,
                freeze_check_enabled=freeze_check_enabled,
                loop_check_enabled=loop_check_enabled,
            )
        finally:
            self._end_single_stream_check_operation()


    def _check_single_stream_reserved(
        self,
        stream_id: int,
        *,
        persist: bool = True,
        blank_check_enabled: bool = False,
        freeze_check_enabled: bool = False,
        loop_check_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Run an already-reserved one-off quality probe.

        This intentionally does not require the stream to be assigned to a
        channel. It measures and optionally persists stream_stats only; it does
        not run regex matching, channel reordering, channel visibility changes,
        or dead-stream removal.
        """
        import time as time_module

        start_time = time_module.time()
        started_monotonic = time_module.monotonic()

        try:
            try:
                stream_id_int = int(stream_id)
            except (TypeError, ValueError):
                return {
                    'success': False,
                    'error': 'invalid_stream_id',
                    'message': 'stream_id must be an integer',
                }

            failed_connectivity = self._require_quality_check_connectivity(
                phase='single_stream_preflight',
                update_progress=False,
            )
            if failed_connectivity is not None:
                payload = self._connectivity_abort_payload(failed_connectivity)
                payload.update({
                    'success': False,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                })
                return payload

            udi = self._checker_get_udi_manager()
            stream_data = udi.get_stream_by_id(stream_id_int)
            if not stream_data:
                return {
                    'success': False,
                    'error': 'stream_not_found',
                    'message': f'Stream {stream_id_int} was not found in the Dispatcharr stream cache',
                    'stream_id': stream_id_int,
                }

            stream_url = stream_data.get('url') or stream_data.get('stream_url')
            if not stream_url:
                return {
                    'success': False,
                    'error': 'stream_missing_url',
                    'message': f'Stream {stream_id_int} has no URL to probe',
                    'stream_id': stream_id_int,
                }

            stream_name = stream_data.get('name') or stream_data.get('stream_name') or f'Stream {stream_id_int}'
            analysis_params = self.config.get('stream_analysis', {}) or {}

            logger.info(
                "Starting one-off stream check for stream_ref=%s (persist=%s, blank=%s, freeze=%s, loop=%s)",
                _audit_ref('stream', stream_id_int),
                bool(persist),
                bool(blank_check_enabled),
                bool(freeze_check_enabled),
                bool(loop_check_enabled),
            )

            bitrate_recheck_enabled = self._is_bitrate_recheck_enabled()
            initial_results = self._run_capacity_limited_stream_probes(
                [stream_data],
                udi=udi,
                ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                timeout=analysis_params.get('timeout', 30),
                retries=analysis_params.get('retries', 1),
                retry_delay=analysis_params.get('retry_delay', 10),
                user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                blank_check_enabled=bool(blank_check_enabled),
                blank_check_min_duration=analysis_params.get('blank_check_min_duration', 2.0),
                blank_check_pixel_threshold=analysis_params.get('blank_check_pixel_threshold', 0.10),
                blank_check_ratio_threshold=analysis_params.get('blank_check_ratio_threshold', 0.80),
                freeze_check_enabled=bool(freeze_check_enabled),
                freeze_check_min_duration=analysis_params.get('freeze_check_min_duration', 5.0),
                freeze_check_noise_threshold=analysis_params.get('freeze_check_noise_threshold', 0.001),
                freeze_check_ratio_threshold=analysis_params.get('freeze_check_ratio_threshold', 0.80),
                hardware_acceleration=analysis_params.get('hardware_acceleration'),
                defer_missing_bitrate_retry=bitrate_recheck_enabled,
                retry_missing_bitrate=bitrate_recheck_enabled,
            )

            if self.abort_current_check.is_set():
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }
            if not initial_results:
                return {
                    'success': False,
                    'error': 'stream_analysis_no_result',
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }

            analyzed = initial_results[0]
            if analyzed.get('provider_limit_skipped'):
                return {
                    'success': False,
                    'error': 'provider_capacity_unavailable',
                    'reason_detail': (
                        analyzed.get('skipped_reason')
                        or analyzed.get('reason_detail')
                        or 'provider_capacity_unavailable'
                    ),
                    'stream_id': stream_id_int,
                    'stream_name': stream_name,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                    'analysis': analyzed,
                }

            def recheck_single_stream_bitrate(_stream, _initial):
                recheck_results = self._run_capacity_limited_stream_probes(
                    [stream_data],
                    udi=udi,
                    ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                    timeout=analysis_params.get('timeout', 30),
                    retries=0,
                    retry_delay=0,
                    user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                    stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                    blank_check_enabled=False,
                    freeze_check_enabled=False,
                    hardware_acceleration=analysis_params.get('hardware_acceleration'),
                    defer_missing_bitrate_retry=False,
                )
                return recheck_results[0] if recheck_results else None

            self._run_deferred_bitrate_rechecks(
                [analyzed],
                {stream_id_int: stream_data},
                recheck_single_stream_bitrate,
                abort_event=self.abort_current_check,
            )
            if self.abort_current_check.is_set():
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }
            self._apply_previous_bitrate_fallback(analyzed, stream_data)

            analyzed['m3u_account_id'] = (
                stream_data.get('m3u_account_id')
                or stream_data.get('m3u_account')
                or stream_data.get('m3u_account_id_id')
            )
            analyzed['run_mode'] = 'single_stream_check'

            threshold_config = dict(self.config.get('dead_stream_handling', {}) or {})
            if blank_check_enabled:
                threshold_config['treat_blank_as_dead'] = True
            if freeze_check_enabled:
                threshold_config['treat_freeze_as_dead'] = True

            dead_result = self._is_stream_dead(analyzed, threshold_config=threshold_config)
            self._apply_quality_classification(analyzed, dead_result)
            is_dead, dead_reason = dead_result
            analyzed['score'] = self._calculate_stream_score(analyzed)

            if loop_check_enabled:
                analysis_params_lp = self.config.get('stream_analysis', {}) or {}
                self._run_loop_probes(
                    [analyzed],
                    user_agent=analysis_params_lp.get('user_agent', 'VLC/3.0.14'),
                    loop_penalty=0.0,
                    probe_duration=analysis_params_lp.get('max_loop_duration', 120) * 3,
                    hardware_acceleration=analysis_params_lp.get('hardware_acceleration'),
                )

            if self.abort_current_check.is_set():
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }

            stats_payload = self._prepare_stream_stats_for_batch(analyzed)
            # Linearize the final persistence decision against clear/abort.
            # Holding the service lock through the optional write makes an
            # abort either win before persistence or belong to later work.
            with self.lock:
                commit_aborted = self.abort_current_check.is_set()
                persisted = False
                if not commit_aborted and persist:
                    persisted = self._update_stream_stats(analyzed)
            if commit_aborted:
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }

            duration_seconds = round(time_module.monotonic() - started_monotonic, 2)
            return {
                'success': True,
                'stream_id': stream_id_int,
                'stream_name': stream_name,
                'run_mode': 'single_stream_check',
                'persisted': bool(persisted),
                'persist_requested': bool(persist),
                'duration_seconds': duration_seconds,
                'dead': bool(is_dead),
                'dead_reason': dead_reason,
                'stats_payload': stats_payload.get('stream_stats', {}) if stats_payload else {},
                'analysis': analyzed,
            }

        except Exception as exc:
            logger.error("Error checking single stream %s: %s", stream_id, exc, exc_info=True)
            return {
                'success': False,
                'error': 'single_stream_check_failed',
                'stream_id': stream_id,
            }

