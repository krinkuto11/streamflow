"""Loop probes responsibilities for the shared StreamCheckerService instance."""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional
from apps.core.log_sanitizer import scrub_urls, stream_ref

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerLoopProbesMixin:
    def _run_loop_probes(self, analyzed_streams: list, user_agent: str = 'VLC/3.0.14', loop_penalty: float = 0.0,
                         probe_duration: int = 360, hardware_acceleration: Optional[dict] = None,
                         channel_id: int = 0, channel_name: str = '',
                         streams_detail: Optional[list] = None,
                         profile_progress_context: Optional[Dict[str, Any]] = None,
                         global_limit_override: Optional[int] = None) -> None:
        """
        Run loop detection probes on eligible streams in parallel with
        per-account concurrent limits, then write results back into each
        stream's analyzed dict.

        Eligibility criteria (both must be met):
          1. score >= LOOP_PROBE_SCORE_THRESHOLD (stream is healthy)
          2. stream is in the top LOOP_PROBE_TOP_PERCENTILE of all scored streams

        Dead streams (score == 0) and cached streams are never probed.

        Parallelism uses AccountStreamLimiter directly rather than
        SmartStreamScheduler to avoid:
          - Progress/start callback conflicts with the quality analysis UI
          - Result-shape mismatch (probe returns a tuple, not a dict)

        Each long-running probe reserves both its aggregate account slot and a
        concrete profile slot. The URL is rebuilt from the raw UDI stream with
        that reserved profile, and the probe remains preemptible when a real
        viewer needs either capacity limit.

        Account ID comes from the UDI stream record ('m3u_account_id' column,
        mapped to 'm3u_account' integer expected by AccountStreamLimiter).

        Results written into each analyzed dict (always present after this call):
          analyzed['loop_detected']      True / False / None (not probed / error)
          analyzed['loop_duration_secs'] float or None
          analyzed['loop_probe_ran']     True / False

        After all probes complete, applies loop_penalty to the score of any
        confirmed looping stream (loop_detected is True). Score is floored at
        0.0 — a looping stream is still better than no stream.

        Args:
            analyzed_streams: List of analyzed stream dicts, each with 'score' set.
            user_agent:       HTTP User-Agent forwarded to FFmpeg.
            loop_penalty:     Negative float (e.g. -0.25) subtracted from score
                              of looping streams. 0.0 = no penalty.
            probe_duration:   Seconds to run each FFmpeg probe. Derived from the
                              global max_loop_duration * 3. Clamped by
                              _probe_stream_for_loops to [60, 720]. Default 360.
            channel_id:       Channel being checked — used for progress reporting.
            channel_name:     Channel name — used for progress reporting.
            streams_detail:   Snapshot of stream_statuses values from the calling
                              check method. Used to build probe_detail for live
                              frontend grid updates. None on paths where
                              stream_statuses is not available.
            profile_progress_context: Optional Current Progress context to
                              preserve across loop-probe UI updates.
            global_limit_override: Optional per-channel worker limit inherited
                              from the quality-analysis path. Legacy sequential
                              checks pass one; normal concurrent checks pass None.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from apps.stream.stream_check_utils import _probe_stream_for_loops
        from apps.stream.concurrent_stream_limiter import (
            get_account_limiter,
            initialize_account_limits,
        )

        LOOP_PROBE_SCORE_THRESHOLD = 0.5
        LOOP_PROBE_TOP_PERCENTILE  = 0.25   # top 25%

        # Initialise loop fields on every stream so callers can always read them
        for s in analyzed_streams:
            s.setdefault('loop_detected', None)
            s.setdefault('loop_duration_secs', None)
            s.setdefault('loop_probe_ran', False)

        # Build candidate pool: alive, scored at or above threshold, not cached
        candidates = [
            s for s in analyzed_streams
            if s.get('score', 0) >= LOOP_PROBE_SCORE_THRESHOLD
            and s.get('status') != 'cached'
        ]

        if not candidates:
            logger.info("[loop-probe] No streams meet eligibility criteria — skipping all probes")
            return

        # Rank by score descending, take top percentile (minimum 1)
        candidates_sorted = sorted(candidates, key=lambda s: s.get('score', 0), reverse=True)
        cutoff  = max(1, int(len(candidates_sorted) * LOOP_PROBE_TOP_PERCENTILE))
        eligible = candidates_sorted[:cutoff]

        total = len(eligible)
        logger.info(
            f"[loop-probe] {total} stream(s) eligible for loop probe "
            f"(top {int(LOOP_PROBE_TOP_PERCENTILE * 100)}% of {len(candidates_sorted)} "
            f"scoring >= {LOOP_PROBE_SCORE_THRESHOLD}) — running in parallel"
        )

        account_limiter = get_account_limiter()
        udi = self._checker_get_udi_manager()
        if not self._initialize_provider_probe_account_inventory(
            udi=udi,
            limiter=account_limiter,
            initialize_account_limits=initialize_account_limits,
            operation_label='Loop probe',
        ):
            logger.warning(
                "[loop-probe] Provider account inventory unavailable; "
                "skipping every loop candidate"
            )
            return

        if global_limit_override is not None:
            global_limit = max(1, int(global_limit_override))
        else:
            concurrent_enabled = bool(self.config.get('concurrent_streams.enabled', True))
            configured_global_limit = self.config.get('concurrent_streams.global_limit', 10)
            try:
                global_limit = max(1, int(configured_global_limit)) if concurrent_enabled else 1
            except (TypeError, ValueError):
                global_limit = 10 if concurrent_enabled else 1

        results_lock = threading.Lock()
        completed = [0]
        progress_context = dict(profile_progress_context or {})
        abort_event = getattr(self, 'abort_current_check', None)

        def abort_requested() -> bool:
            is_set = getattr(abort_event, 'is_set', None)
            return bool(is_set()) if callable(is_set) else False

        # Build probe_detail from the streams_detail snapshot passed in by the
        # calling check method. Keyed by integer stream id (same as stream_statuses).
        # Only built when streams_detail is available — the grid stays frozen
        # otherwise but the probes still run correctly.
        probe_detail: dict = {}
        if streams_detail:
            for entry in streams_detail:
                sid = entry.get('id') or entry.get('stream_id')
                if sid is not None:
                    probe_detail[sid] = dict(entry)  # shallow copy

        # Mark eligible streams as 'probing' and stamp started_at
        eligible_ids = {s.get('stream_id') for s in eligible}
        probe_start = datetime.now().isoformat()
        for sid, entry in probe_detail.items():
            if sid in eligible_ids:
                entry['status'] = 'probing'
                entry['started_at'] = probe_start

        # Emit phase-entry progress update so frontend transitions to loop phase
        if channel_id and probe_detail:
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=total,
                status='analyzing',
                step='Loop testing',
                step_detail=f'Probing {total} stream(s) for looping content',
                streams_detail=list(probe_detail.values()),
                stream_duration=probe_duration,
                **progress_context,
            )

        def finish_probe(stream: dict, completion_status: str) -> None:
            """Finalize one eligible item so no Progress row remains probing."""
            stream_id = stream.get('stream_id')
            stream_name = stream.get('stream_name', 'Unknown')
            stream_audit_ref = stream_ref(stream_id, stream.get('stream_url', ''))
            with results_lock:
                completed[0] += 1
                if probe_detail and stream_id in probe_detail:
                    loop_result = stream.get('loop_detected')
                    probe_detail[stream_id]['status'] = (
                        'loop_detected' if loop_result is True else completion_status
                    )
                if channel_id and probe_detail:
                    self.progress.update(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=completed[0],
                        total=total,
                        status='analyzing',
                        step='Loop testing',
                        step_detail=f'Completed {completed[0]}/{total}: {stream_name}',
                        streams_detail=list(probe_detail.values()),
                        stream_duration=probe_duration,
                        **progress_context,
                    )
                logger.info(
                    f"[loop-probe] Completed {completed[0]}/{total}: {stream_audit_ref}"
                )

        def acquire_account_with_abort(account_id: Optional[int]) -> tuple[bool, str]:
            """Poll account capacity for up to 60s while honoring manual abort."""
            deadline = time.monotonic() + 60.0
            last_reason = 'timeout'
            while True:
                if abort_requested():
                    return False, 'aborted'
                acquired, reason = account_limiter.acquire(account_id, timeout=0)
                if acquired:
                    return True, reason
                last_reason = reason
                if reason == 'provider_profile_unavailable':
                    # Missing provider authority cannot recover during this
                    # operation. Do not turn an authoritative empty inventory
                    # into a 60-second wait; custom streams use account_id=None
                    # and still pass through the normal reservation path.
                    return False, last_reason
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, last_reason
                wait_seconds = min(0.5, remaining)
                wait_for_abort = getattr(abort_event, 'wait', None)
                if callable(wait_for_abort):
                    if wait_for_abort(wait_seconds):
                        return False, 'aborted'
                else:
                    time.sleep(wait_seconds)

        def _probe_one(stream: dict) -> None:
            """Run one viewer-preemptible loop probe with account/profile reservations."""
            stream_url  = stream.get('stream_url', '')
            stream_name = stream.get('stream_name', 'Unknown')
            stream_id   = stream.get('stream_id')
            score       = stream.get('score', 0)
            stream_audit_ref = stream_ref(stream_id, stream_url)
            completion_status = 'skipped'

            if abort_requested():
                finish_probe(stream, 'aborted')
                return

            # Resolve numeric account ID from UDI.
            # analyzed dicts carry stream_id from quality analysis — use that
            # to look up the raw stream record which has m3u_account_id.
            account_id = None
            raw_stream = None
            try:
                raw_stream = udi.get_stream_by_id(int(stream_id)) if stream_id else None
                if raw_stream:
                    # SQL storage uses m3u_account_id; AccountStreamLimiter
                    # expects the integer under the key 'm3u_account'
                    account_id = raw_stream.get('m3u_account_id') or raw_stream.get('m3u_account')
            except Exception as e:
                logger.warning(f"[loop-probe:{stream_audit_ref}] Raw UDI lookup failed: {e}")

            if not isinstance(raw_stream, dict):
                logger.warning(
                    f"[loop-probe:{stream_audit_ref}] Skipping stream - raw UDI record unavailable"
                )
                finish_probe(stream, completion_status)
                return

            tag = stream_audit_ref

            reservation_stream = dict(raw_stream)
            if account_id not in (None, ''):
                reservation_stream.setdefault('m3u_account_id', account_id)
                reservation_stream.setdefault('m3u_account', account_id)

            # Acquire account slot — same mechanism used by quality analysis.
            # Timeout of 60s: if the account is saturated (e.g. live viewers
            # consuming all slots) we skip rather than block indefinitely.
            try:
                acquired_account, reason = acquire_account_with_abort(account_id)
            except Exception as e:
                logger.error(f"[loop-probe:{tag}] Account reservation failed: {e}")
                finish_probe(stream, 'error')
                return
            if not acquired_account:
                completion_status = 'aborted' if reason == 'aborted' else 'skipped'
                logger.info(
                    f"[loop-probe:{tag}] Skipping stream - "
                    f"account slot unavailable ({reason})"
                )
                finish_probe(stream, completion_status)
                return

            acquired_profile = None
            preempted_for_viewer = threading.Event()
            manual_abort_observed = threading.Event()
            preemption_token = object()
            preemption_claimed = False
            try:
                if abort_requested():
                    completion_status = 'aborted'
                    return
                profile_acquired, reason, acquired_profile, probe_url = (
                    account_limiter.reserve_profile_for_stream_with_url(reservation_stream)
                )
                if not profile_acquired:
                    logger.info(
                        f"[loop-probe:{tag}] Skipping stream - "
                        f"profile slot unavailable ({reason})"
                    )
                    return

                if not isinstance(probe_url, str) or not probe_url:
                    logger.warning(
                        f"[loop-probe:{tag}] Skipping stream - reserved profile has no usable URL"
                    )
                    return

                def should_abort_for_viewer() -> bool:
                    nonlocal preemption_claimed
                    if abort_requested():
                        manual_abort_observed.set()
                        return True
                    try:
                        should_preempt = account_limiter.should_preempt_profile_for_viewer(
                            acquired_profile,
                            account_id=account_id,
                            reservation_token=preemption_token,
                        )
                        if should_preempt:
                            preemption_claimed = True
                    except Exception as e:
                        # Fail safe: an unknown capacity state must not keep a
                        # 60-720 second provider probe alive ahead of viewers.
                        logger.warning(
                            f"[loop-probe:{tag}] Viewer preemption check failed: {e}"
                        )
                        should_preempt = True
                    if should_preempt:
                        preempted_for_viewer.set()
                    return should_preempt

                logger.info(
                    f"[loop-probe:{tag}] Probing stream "
                    f"(score: {score:.2f})"
                )
                loop_detected, loop_duration, _frames = _probe_stream_for_loops(
                    url=probe_url,
                    stream_tag=tag,
                    probe_duration=probe_duration,
                    user_agent=user_agent,
                    hardware_acceleration=hardware_acceleration,
                    should_abort=should_abort_for_viewer,
                )
                if manual_abort_observed.is_set() or abort_requested():
                    completion_status = 'aborted'
                    logger.info(f"[loop-probe:{tag}] Probe stopped by manual abort")
                    return
                if preempted_for_viewer.is_set():
                    completion_status = 'viewer_preempted'
                    logger.info(
                        f"[loop-probe:{tag}] Probe preempted because real viewer capacity is needed"
                    )
                    return
                stream['loop_detected']      = loop_detected
                stream['loop_duration_secs'] = loop_duration
                stream['loop_probe_ran']     = True
                completion_status = 'completed'

            except Exception as e:
                completion_status = 'error'
                logger.error(
                    f"[loop-probe:{tag}] Probe failed: {scrub_urls(e)}"
                )
                # loop_detected remains None — distinguishable from clean (False)
                # or detected (True)
            finally:
                if acquired_profile is not None:
                    try:
                        account_limiter.release_profile(acquired_profile)
                    except Exception as e:
                        logger.warning(f"[loop-probe:{tag}] Profile release failed: {e}")
                try:
                    account_limiter.release(account_id)
                except Exception as e:
                    logger.warning(f"[loop-probe:{tag}] Account release failed: {e}")
                if preemption_claimed:
                    try:
                        account_limiter.release_viewer_preemption_claim(preemption_token)
                    except Exception as e:
                        logger.warning(f"[loop-probe:{tag}] Preemption claim release failed: {e}")
                finish_probe(stream, completion_status)

        with ThreadPoolExecutor(max_workers=global_limit) as executor:
            futures = {}
            for stream in eligible:
                if abort_requested():
                    finish_probe(stream, 'aborted')
                    continue
                futures[executor.submit(_probe_one, stream)] = stream
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    stream = futures[future]
                    logger.error(
                        f"[loop-probe] Unhandled error for stream "
                        f"{stream_ref(stream.get('stream_id'), stream.get('stream_url'))}: {scrub_urls(e)}"
                    )

        logger.info(
            f"[loop-probe] Probe phase complete — {completed[0]}/{total} eligible items finalized"
        )

        # Apply score penalty to confirmed looping streams.
        # Only fires when loop_penalty is non-zero and loop_detected is True.
        # Score is floored at 0.0 — looping is bad but the stream still exists.
        if loop_penalty < 0.0:
            penalised = 0
            for stream in analyzed_streams:
                if stream.get('loop_detected') is True:
                    original = stream.get('score', 0.0)
                    stream['score'] = round(max(0.0, original + loop_penalty), 2)
                    stream['loop_score_penalty'] = loop_penalty
                    penalty_ref = stream_ref(stream.get('stream_id'), stream.get('stream_url'))
                    logger.info(
                        f"[loop-probe] Penalty applied to {penalty_ref}: "
                        f"{original:.2f} → {stream['score']:.2f} (penalty={loop_penalty:+.2f})"
                    )
                    penalised += 1
            if penalised:
                logger.info(f"[loop-probe] Score penalty applied to {penalised} looping stream(s)")

