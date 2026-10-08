"""Classification responsibilities for the shared StreamCheckerService instance."""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from apps.core.log_sanitizer import audit_ref as _audit_ref
from apps.automation.automation_config_manager import get_automation_config_manager
from apps.core.stream_stats_utils import calculate_channel_averages, is_stream_dead as utils_is_stream_dead

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerClassificationMixin:
    def _build_threshold_config_from_profile(self, stream_checking: Dict[str, Any]) -> Dict[str, Any]:
        """Build a threshold config dict from an already-resolved stream_checking block.

        Called once per channel run so the per-stream _is_stream_dead() calls
        don't have to re-resolve the profile from the database.  This also
        ensures that a forced_profile_id (e.g. from the multi-period picker)
        is honoured — previously _is_stream_dead() re-derived the profile from
        the channel's period assignments, ignoring any explicit picker selection.

        Args:
            stream_checking: The stream_checking sub-dict from the resolved profile.

        Returns:
            A config dict suitable for passing to utils_is_stream_dead() as the
            second argument.  Only non-zero thresholds are included.
        """
        config: Dict[str, Any] = {}
        min_res = stream_checking.get('min_resolution', 'any')
        if min_res in ('2160p', '4k'):
            config['min_resolution_width'], config['min_resolution_height'] = 3840, 2160
        elif min_res == '1080p':
            config['min_resolution_width'], config['min_resolution_height'] = 1920, 1080
        elif min_res == '720p':
            config['min_resolution_width'], config['min_resolution_height'] = 1280, 720
        elif min_res == '480p':
            config['min_resolution_width'], config['min_resolution_height'] = 854, 480
        elif min_res == '360p':
            config['min_resolution_width'], config['min_resolution_height'] = 640, 360

        min_bitrate = stream_checking.get('min_bitrate', 0)
        if min_bitrate and min_bitrate > 0:
            config['min_bitrate_kbps'] = min_bitrate

        min_fps = stream_checking.get('min_fps', 0)
        if min_fps and min_fps > 0:
            config['min_fps'] = min_fps

        # A single profile switch controls blank handling: when blank checks run,
        # detected blank streams are treated as dead. The legacy
        # treat_blank_as_dead value is intentionally ignored so older profiles
        # with it set to False do not silently keep blanks alive.
        config['treat_blank_as_dead'] = stream_checking.get('blank_check_enabled') is True
        config['treat_freeze_as_dead'] = stream_checking.get('freeze_check_enabled') is True

        return config


    def _is_stream_dead(self, stream_data: Dict[str, Any], channel_id: Optional[int] = None, threshold_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """
        Check if a stream should be considered dead based on profile or global settings.

        This method uses categorization logic:
        - 'offline': truly dead (0x0 resolution, 0 bitrate)
        - 'low_quality': dead based on quality thresholds

        Args:
            stream_data: Dictionary containing stream statistics
            channel_id: Optional channel ID to look up profile-specific thresholds.
                        Ignored when threshold_config is provided.
            threshold_config: Pre-built threshold dict from _build_threshold_config_from_profile().
                              When supplied, profile re-resolution is skipped entirely.
                              This ensures forced_profile_id selections are honoured.

        Returns:
            Tuple of (is_dead: bool, reason: str).
            reason values: 'offline', 'low_quality', 'unstable', 'none'.
        """
        # Default configuration
        dead_stream_config = self.config.get('dead_stream_handling', {})
        profile_config = {}

        # Fast path: caller already resolved the profile and built the threshold dict.
        # Skip the expensive per-stream profile re-resolution entirely.
        if threshold_config is not None:
            check_config = threshold_config
        # Slow path: resolve profile from channel_id (legacy / external callers).
        elif channel_id is not None:
            try:
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()

                # Get effective profile
                udi = self._checker_get_udi_manager()
                channel = udi.get_channel_by_id(channel_id)
                group_id = channel.get('channel_group_id') if channel else None
                config = automation_config.get_effective_configuration(channel_id, group_id)
                profile = config.get('profile') if config else None

                if profile:
                    stream_checking = profile.get('stream_checking', {})
                    if stream_checking.get('enabled', False):
                        # Construct config from profile settings
                        # Convert min_res string (e.g., '1080p') to dimensions
                        min_res = stream_checking.get('min_resolution', '0x0')
                        if min_res == '2160p' or min_res == '4k':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 3840, 2160
                        elif min_res == '1080p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 1920, 1080
                        elif min_res == '720p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 1280, 720
                        elif min_res == '480p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 854, 480
                        elif min_res == '360p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 640, 360

                        if 'min_bitrate' in stream_checking:
                            profile_config['min_bitrate_kbps'] = stream_checking['min_bitrate']

                        if 'min_fps' in stream_checking:
                            profile_config['min_fps'] = stream_checking['min_fps']

                        # Use profile config if available
                        check_config = profile_config
                    else:
                        # Stream matching enabled but checker disabled in profile?
                        # Fallback to global or basic
                        check_config = dead_stream_config if dead_stream_config.get('enabled', True) else {'min_resolution_width': 0, 'min_resolution_height': 0, 'min_bitrate_kbps': 0}
                else:
                    check_config = dead_stream_config
            except Exception as e:
                logger.warning(f"Error fetching profile for dead stream check: {e}")
                check_config = dead_stream_config
        else:
            check_config = dead_stream_config

        # If global handling is disabled and no profile was found, use basic check (absolute failures only)
        if not check_config.get('enabled', True) and not profile_config:
            check_config = {
                'min_resolution_width': 0,
                'min_resolution_height': 0,
                'min_bitrate_kbps': 0,
                'min_score': 0
            }

        # Use centralized utility for the check
        return utils_is_stream_dead(stream_data, check_config)


    @staticmethod
    def _apply_quality_classification(stream_data: Dict[str, Any], result: Any) -> None:
        """Stamp machine-readable quality classification details onto a stream."""
        reason = getattr(result, 'reason', result[1] if isinstance(result, tuple) and len(result) > 1 else 'none')
        reason_detail = getattr(result, 'reason_detail', reason)
        details = getattr(result, 'details', {}) or {}

        stream_data['quality_reason'] = reason
        stream_data['quality_reason_detail'] = reason_detail
        stream_data['quality_reason_context'] = details
        if reason == 'none' and stream_data.get('measurement_incomplete_reason') in {
            'missing_bitrate',
            'missing_bitrate_after_recheck',
        }:
            incomplete_reason = stream_data['measurement_incomplete_reason']
            incomplete_context = stream_data.get('measurement_incomplete_context') or {}
            stream_data['quality_reason'] = incomplete_reason
            stream_data['quality_reason_detail'] = incomplete_reason
            stream_data['quality_reason_context'] = incomplete_context
        if result and reason != 'none':
            stream_data['dead_reason'] = reason
            stream_data['dead_reason_detail'] = reason_detail
            stream_data['dead_reason_context'] = details


    def _calculate_channel_averages(self, analyzed_streams: List[Dict], dead_stream_ids: set) -> Dict[str, str]:
        """Calculate channel-level average statistics from analyzed streams.

        Uses centralized utility function for consistent average calculation.

        Args:
            analyzed_streams: List of analyzed stream dictionaries
            dead_stream_ids: Set of stream IDs that are marked as dead

        Returns:
            Dictionary with avg_resolution, avg_bitrate, and avg_fps
        """
        return calculate_channel_averages(analyzed_streams, dead_stream_ids)


    def _log_blank_detection_summary(
        self,
        channel_id: int,
        _channel_name: str,
        analyzed_streams: List[Dict],
        dead_stream_ids: Optional[Set[int]] = None,
        dead_stream_removal_enabled: Optional[bool] = None,
    ) -> None:
        """Log a URL-free blank-detection summary for post-run audits."""
        probed_streams = [
            stream for stream in analyzed_streams
            if stream.get('blank_probe_ran') and stream.get('status') != 'cached'
        ]
        if not probed_streams:
            return

        blank_streams = [stream for stream in probed_streams if stream.get('blank_detected')]
        clean_count = len(probed_streams) - len(blank_streams)

        def _metric(stream: Dict, key: str) -> float:
            try:
                return float(stream.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        max_ratio_stream = max(probed_streams, key=lambda stream: _metric(stream, 'blank_ratio'))
        channel_ref = _audit_ref('channel', channel_id)
        logger.info(
            f"[blank-detect] Channel summary: channel_ref={channel_ref}, "
            f"probed={len(probed_streams)}, clean={clean_count}, "
            f"blank={len(blank_streams)}, "
            f"max_ratio={_metric(max_ratio_stream, 'blank_ratio'):.3f}, "
            f"max_blank_duration={_metric(max_ratio_stream, 'blank_duration_secs'):.1f}s"
        )

        dead_stream_ids = dead_stream_ids or set()
        removal_enabled = bool(dead_stream_removal_enabled)

        for stream in blank_streams:
            stream_id = stream.get('stream_id')
            marked_dead = stream_id in dead_stream_ids
            dead_reason = stream.get('dead_reason') or ('blank' if marked_dead else 'none')
            action = 'remove' if marked_dead and removal_enabled else 'retain'
            logger.warning(
                f"[blank-detect] Blank candidate: channel_ref={channel_ref}, "
                f"stream_ref={_audit_ref('stream', stream_id)}, "
                f"duration={_metric(stream, 'blank_duration_secs'):.1f}s, "
                f"ratio={_metric(stream, 'blank_ratio'):.3f}, "
                f"segments={len(stream.get('blank_segments') or [])}, "
                f"marked_dead={marked_dead}, reason={dead_reason}, "
                f"removal_enabled={removal_enabled}, action={action}"
            )


    def _log_freeze_detection_summary(
        self,
        channel_id: int,
        _channel_name: str,
        analyzed_streams: List[Dict],
        dead_stream_ids: Optional[Set[int]] = None,
        dead_stream_removal_enabled: Optional[bool] = None,
    ) -> None:
        """Log a URL-free freeze-detection summary for post-run audits."""
        probed_streams = [
            stream for stream in analyzed_streams
            if stream.get('freeze_probe_ran') and stream.get('status') != 'cached'
        ]
        if not probed_streams:
            return

        frozen_streams = [stream for stream in probed_streams if stream.get('freeze_detected')]
        clean_count = len(probed_streams) - len(frozen_streams)

        def _metric(stream: Dict, key: str) -> float:
            try:
                return float(stream.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        max_ratio_stream = max(probed_streams, key=lambda stream: _metric(stream, 'freeze_ratio'))
        channel_ref = _audit_ref('channel', channel_id)
        logger.info(
            f"[freeze-detect] Channel summary: channel_ref={channel_ref}, "
            f"probed={len(probed_streams)}, clean={clean_count}, "
            f"frozen={len(frozen_streams)}, "
            f"max_ratio={_metric(max_ratio_stream, 'freeze_ratio'):.3f}, "
            f"max_freeze_duration={_metric(max_ratio_stream, 'freeze_duration_secs'):.1f}s"
        )

        dead_stream_ids = dead_stream_ids or set()
        removal_enabled = bool(dead_stream_removal_enabled)

        for stream in frozen_streams:
            stream_id = stream.get('stream_id')
            marked_dead = stream_id in dead_stream_ids
            dead_reason = stream.get('dead_reason') or ('freeze' if marked_dead else 'none')
            action = 'remove' if marked_dead and removal_enabled else 'retain'
            logger.warning(
                f"[freeze-detect] Frozen candidate: channel_ref={channel_ref}, "
                f"stream_ref={_audit_ref('stream', stream_id)}, "
                f"duration={_metric(stream, 'freeze_duration_secs'):.1f}s, "
                f"ratio={_metric(stream, 'freeze_ratio'):.3f}, "
                f"segments={len(stream.get('freeze_segments') or [])}, "
                f"marked_dead={marked_dead}, reason={dead_reason}, "
                f"removal_enabled={removal_enabled}, action={action}"
            )


    def _refresh_dead_stream_reason_if_needed(
        self,
        stream_url: str,
        stream_id: int,
        stream_name: str,
        channel_id: int,
        reason: str,
        blank_detected: bool = False,
        freeze_detected: bool = False,
    ) -> bool:
        """Refresh stale dead-stream reasons after a checked stream gets a newer verdict."""
        if not stream_url or not reason or reason == 'none':
            return False

        try:
            current_reason = None
            if hasattr(self.dead_streams_tracker, 'get_dead_reason'):
                current_reason = self.dead_streams_tracker.get_dead_reason(stream_url)
            if current_reason == reason:
                return False

            update_reason = getattr(self.dead_streams_tracker, 'update_dead_reason', None)
            if not callable(update_reason):
                return False
            updated = update_reason(stream_url, reason, channel_id=channel_id)

            if updated and (blank_detected or freeze_detected):
                detection_label = 'blank' if blank_detected else 'freeze'
                logger.warning(
                    f"[{detection_label}-detect] Stream dead reason updated: "
                    f"channel_ref={_audit_ref('channel', channel_id)}, "
                    f"stream_ref={_audit_ref('stream', stream_id)}, "
                    f"reason={reason}"
                )
            return bool(updated)
        except Exception as exc:
            logger.warning("Failed to refresh dead stream reason for stream %s: %s", stream_id, exc)
            return False


    def _get_resolution_product(self, stream_data: Dict) -> int:
        """Get resolution product (width * height) from stream data."""
        res = stream_data.get('resolution', '')
        if 'x' in str(res):
            try:
                width, height = map(int, str(res).split('x'))
                return width * height
            except: pass
        return 0


    def _prepare_playback_scoring(self, scoring_weights: Optional[Dict], channel_id: int) -> Optional[Dict]:
        """Resolve one immutable history snapshot per channel, never per probe."""
        if not scoring_weights or scoring_weights.get('use_playback_stability') is not True:
            return scoring_weights
        prepared = dict(scoring_weights)
        # Never accept an injected internal snapshot from a saved profile.
        prepared['_playback_stability'] = {}
        try:
            from apps.stream.playback_stability_service import get_playback_stability_service
            prepared['_playback_stability'] = get_playback_stability_service().scoring_snapshot(channel_id)
        except Exception:
            logger.warning('Playback Stability evidence unavailable; using unchanged quality scoring')
        return prepared

    def _calculate_stream_score(self, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute', scoring_weights: Dict = None) -> float:
        """Calculate a quality score for a stream based on analysis.

        Applies M3U account priority matching the order in the channel's Automation Profile.

        Args:
            stream_data: Dictionary of stream analysis data
            priority_m3u_ids: List of M3U account IDs in priority order (highest first)
            priority_mode: stream ordering mode; score calculation stays quality-based.
            scoring_weights: Optional per-profile scoring weights. Falls back to global config if not provided.
        """
        # Dead streams always get a score of 0
        stream_data.pop('playback_stability_score', None)
        _dead, _ = self._is_stream_dead(stream_data)
        if _dead:
            return 0.0

        # Use per-profile weights if provided, otherwise fall back to global config
        if scoring_weights is None:
            weights = self.config.get('scoring.weights', {})
            prefer_h265 = self.config.get('scoring.prefer_h265', True)
        else:
            weights = {
                'bitrate': scoring_weights.get('bitrate', 0.35),
                'resolution': scoring_weights.get('resolution', 0.30),
                'fps': scoring_weights.get('fps', 0.15),
                'codec': scoring_weights.get('codec', 0.10),
                'hdr': scoring_weights.get('hdr', 0.10)
            }
            prefer_h265 = scoring_weights.get('prefer_h265', True)

        score = 0.0

        # Bitrate score (0-1, normalized to typical range 1000-8000 kbps)
        bitrate = stream_data.get('bitrate_kbps', 0)
        if self._bitrate_payload_value(bitrate) is None:
            bitrate = stream_data.get('scoring_bitrate_kbps', 0)
        if isinstance(bitrate, (int, float)) and bitrate > 0:
            bitrate_score = min(bitrate / 8000, 1.0)
            score += bitrate_score * weights.get('bitrate', 0.40)

        # Resolution score (0-1)
        resolution = stream_data.get('resolution', 'N/A')
        resolution_score = 0.0
        if 'x' in str(resolution):
            try:
                width, height = map(int, resolution.split('x'))
                # Score based on vertical resolution
                if height >= 2160:
                    resolution_score = 1.0
                elif height >= 1080:
                    resolution_score = 0.85
                elif height >= 720:
                    resolution_score = 0.7
                elif height >= 576:
                    resolution_score = 0.5
                else:
                    resolution_score = 0.3
            except (ValueError, AttributeError):
                pass
        score += resolution_score * weights.get('resolution', 0.35)

        # FPS score (0-1)
        fps = stream_data.get('fps', 0)
        if isinstance(fps, (int, float)) and fps > 0:
            fps_score = min(fps / 60, 1.0)
            score += fps_score * weights.get('fps', 0.15)

        # Codec score (0-1)
        codec = str(stream_data.get('video_codec') or '').lower()
        codec_score = 0.0
        if codec:
            if 'h265' in codec or 'hevc' in codec:
                codec_score = 1.0 if prefer_h265 else 0.8
            elif 'h264' in codec or 'avc' in codec:
                codec_score = 0.8 if prefer_h265 else 1.0
            elif codec != 'n/a':
                codec_score = 0.5
        score += codec_score * weights.get('codec', 0.10)

        # HDR score (0-1)
        # Give full score for HDR10 or HLG, zero for SDR
        hdr_format = stream_data.get('hdr_format')
        hdr_score = 1.0 if hdr_format in ['HDR10', 'HLG'] else 0.0
        score += hdr_score * weights.get('hdr', 0.10)

        return round(self._apply_playback_stability_score(score, stream_data, scoring_weights), 2)

    @staticmethod
    def _apply_playback_stability_score(score: float, stream_data: Dict, scoring_weights: Optional[Dict]) -> float:
        # This is a bounded deduction from the existing score, not a new weight
        # in its denominator. Missing evidence leaves the score exactly intact.
        if scoring_weights and scoring_weights.get('use_playback_stability') is True:
            stability = scoring_weights.get('_playback_stability', {}).get(stream_data.get('stream_id'))
            weight = scoring_weights.get('playback_stability_weight', 0.15)
            if (isinstance(stability, (int, float)) and not isinstance(stability, bool)
                    and 0 <= stability <= 1 and isinstance(weight, (int, float))
                    and not isinstance(weight, bool) and 0 <= weight <= 1):
                score *= 1 - weight * (1 - stability)
                stream_data['playback_stability_score'] = round(stability * 100, 1)
        return score


    def _get_priority_boost(self, stream_id: int, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute') -> float:
        """Calculate priority boost for a stream based on its M3U account priority.

        Args:
            stream_id: The stream ID
            stream_data: Stream data dictionary containing resolution and other info
            priority_m3u_ids: List of M3U account IDs in priority order (highest first)
            priority_mode: legacy boost mode, retained for compatibility.

        Returns:
            Priority boost value
        """
        try:
            if not priority_m3u_ids:
                return 0.0

            # Get stream from UDI to find its M3U account
            udi = self._checker_get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if not stream:
                return 0.0

            m3u_account_id = self._get_stream_m3u_account_id(stream)
            if not m3u_account_id:
                return 0.0

            priority_rank = self._get_priority_account_rank(m3u_account_id, priority_m3u_ids)
            # Check if this account is in the priority list
            if priority_rank is not None:
                # Calculate boost based on position (index)
                # Lower index = higher priority
                index = priority_rank
                total_accounts = len(priority_m3u_ids)

                if priority_mode == 'equal':
                    # Equal Mode
                    # No priority boost, ranking matches quality exactly
                    boost = 0.0
                    logger.debug(f"Applying equal priority (no boost) to stream {stream_id}")
                elif priority_mode == 'same_resolution':
                    # Same Resolution (Tie-breaker) Mode
                    # Small boost (0.05 per step) to break ties in quality
                    # Example separation: 0.15 max (less than resolution tier gap)
                    boost = 0.05 * (total_accounts - index)
                    logger.debug(f"Applying tie-breaker priority boost of {boost} to stream {stream_id}")
                else:
                    # Absolute Mode (Default)
                    # Massive boost to override quality differences
                    # Boost formula: Base 10 + (inverted index count)
                    boost = 10.0 + (total_accounts - index)
                    logger.debug(f"Applying absolute priority boost of {boost} to stream {stream_id}")

                return boost

            return 0.0
        except Exception as e:
            logger.error(f"Error calculating priority boost for stream {stream_id}: {e}")
            return 0.0


    def _get_resolution_tier(self, resolution: str) -> int:
        """Map resolution string to a numeric tier (0-5, lower is better)."""
        if not resolution or 'x' not in str(resolution):
            return 5 # Unknown/N/A

        try:
            # Handle list/tuple format if resolution was already parsed elsewhere
            if isinstance(resolution, (list, tuple)):
                height = int(resolution[1])
            else:
                width, height = map(int, str(resolution).split('x'))

            if height >= 2160: return 0 # 4K
            if height >= 1080: return 1 # 1080p
            if height >= 720:  return 2 # 720p
            if height >= 576:  return 3 # 576p/SD
            return 4 # Low resolution
        except (ValueError, AttributeError, IndexError):
            return 5


    def _generate_stream_sort_key(self, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute') -> Tuple:
        """Generate a lexicographical sort key for a stream based on priority tiers.

        The sort key is a tuple used for ascending sort (lower is better).

        Modes:
        - absolute: AccountRank, ResolutionTier, QualityScore
        - same_resolution: ResolutionTier, AccountRank, QualityScore
        - playlist_score: AccountRank, QualityScore
        - score_playlist: QualityScore, AccountRank
        """
        # 1. Account Rank (0 = highest)
        account_rank = 100
        stream_id = stream_data.get('stream_id')
        if priority_m3u_ids and stream_id:
            udi = self._checker_get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if stream:
                m3u_id = self._get_stream_m3u_account_id(stream)
                priority_rank = self._get_priority_account_rank(m3u_id, priority_m3u_ids)
                if priority_rank is not None:
                    account_rank = priority_rank

        # 2. Resolution Tier (0 = highest)
        res_tier = self._get_resolution_tier(stream_data.get('resolution'))


        # 4. Quality Score (lower is better, so negate the 0-1 scale)
        quality_score = -stream_data.get('score', 0.0)

        if priority_mode == 'same_resolution':
            return (res_tier, account_rank, quality_score)
        elif priority_mode == 'playlist_score':
            # Playlist rank first, then quality score within the same playlist/account.
            return (account_rank, quality_score)
        elif priority_mode == 'score_playlist':
            # Score first, playlist rank only breaks equal-score ties.
            return (quality_score, account_rank)
        elif priority_mode == 'equal':
            # In 'equal' mode, resolution and quality matter, but not M3U account priority
            return (res_tier, quality_score)
        elif priority_mode == 'quality':
            # Score-only mode: sort purely by quality score, ignoring account rank and resolution tier
            return (quality_score,)
        else: # 'absolute' mode
            return (account_rank, res_tier, quality_score)

