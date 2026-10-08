"""Probe result preparation and shared acknowledged statistics writes."""

import logging
from typing import Any, Dict, Optional
from apps.core.api_utils import batch_update_stream_stats

logger = logging.getLogger(__name__)


class StreamStatsWriterMixin:
    def _update_stream_stats(self, stream_data: Dict) -> bool:
        """Update stream stats for a single stream on the server and sync with UDI cache.
        
        This method:
        1. Constructs the stats payload from analyzed stream data
        2. Merges with existing stats on Dispatcharr
        3. PATCHes the updated stats to Dispatcharr
        4. Updates the UDI cache to keep it in sync
        
        This ensures that the UDI cache always reflects the latest stats written to Dispatcharr,
        preventing inconsistencies between changelog data and actual Dispatcharr data.
        """
        stream_id = stream_data.get("stream_id")
        if not stream_id:
            logger.warning("No stream_id in stream data. Skipping stats update.")
            return False
        
        bitrate_value = self._bitrate_payload_value(stream_data.get("bitrate_kbps"))
        preserve_existing_bitrate = self._should_preserve_existing_bitrate(stream_data)

        # Construct the stream stats payload from the analyzed stream data
        stream_stats_payload = {
            "resolution": stream_data.get("resolution"),
            "source_fps": stream_data.get("fps"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
            "hdr_format": stream_data.get("hdr_format"),
            "pixel_format": stream_data.get("pixel_format"),
            "audio_sample_rate": stream_data.get("audio_sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
            "channel_layout": stream_data.get("channel_layout"),
            "audio_bitrate": stream_data.get("audio_bitrate"),
            "ffmpeg_output_bitrate": bitrate_value,
            "bitrate_source": stream_data.get("bitrate_source"),
            "quality_score": stream_data.get("score"),
            "quality_reason": stream_data.get("quality_reason"),
            "quality_reason_detail": stream_data.get("quality_reason_detail"),
            "quality_reason_context": stream_data.get("quality_reason_context"),
            "measurement_incomplete": bool(stream_data.get("measurement_incomplete")),
            "measurement_incomplete_reason": stream_data.get("measurement_incomplete_reason") or "none",
            "measurement_incomplete_context": stream_data.get("measurement_incomplete_context") or {},
            "bitrate_recheck_required": bool(stream_data.get("bitrate_recheck_required")),
            "bitrate_recheck_attempted": bool(stream_data.get("bitrate_recheck_attempted")),
            "bitrate_recheck_outcome": stream_data.get("bitrate_recheck_outcome") or "not_needed",
            "visual_probe_ran": True if stream_data.get("visual_probe_ran") else (False if "visual_probe_ran" in stream_data else None),
            "visual_probe_completed": stream_data.get("visual_probe_completed") if "visual_probe_completed" in stream_data else None,
            "visual_probe_incomplete": stream_data.get("visual_probe_incomplete") if "visual_probe_incomplete" in stream_data else None,
            "visual_probe_incomplete_reason": (
                stream_data.get("visual_probe_incomplete_reason") or "none"
                if "visual_probe_ran" in stream_data or "visual_probe_incomplete_reason" in stream_data
                else None
            ),
            "visual_probe_requested_duration_seconds": stream_data.get(
                "visual_probe_requested_duration_seconds"
            ),
            "visual_probe_minimum_duration_seconds": stream_data.get(
                "visual_probe_minimum_duration_seconds"
            ),
            "visual_probe_duration_seconds": stream_data.get("visual_probe_duration_seconds"),
            "visual_probe_duration_adjusted": stream_data.get("visual_probe_duration_adjusted"),
            "visual_probe_duration_adjustment_reason": stream_data.get(
                "visual_probe_duration_adjustment_reason"
            ),
            "visual_probe_elapsed_time": stream_data.get("visual_probe_elapsed_time"),
            # PRESERVE_FALSE: emit False (not None) so the None-filter below keeps these
            # fields in the payload even when the probe ran but found no loop.
            # Without this, Dispatcharr's PATCH merge leaves a stale loop_probe_ran: true
            # from a previous run in place for streams not probed in the current run.
            "loop_probe_ran": True if stream_data.get("loop_probe_ran") else (False if "loop_probe_ran" in stream_data else None),
            "loop_detected": stream_data.get("loop_detected") if stream_data.get("loop_probe_ran") else (False if "loop_detected" in stream_data else None),
            "loop_duration_secs": stream_data.get("loop_duration_secs") if stream_data.get("loop_detected") else None,
            "loop_score_penalty": stream_data.get("loop_score_penalty"),
            "blank_probe_ran": True if stream_data.get("blank_probe_ran") else (False if "blank_probe_ran" in stream_data else None),
            "blank_detected": stream_data.get("blank_detected") if stream_data.get("blank_probe_ran") else (False if "blank_detected" in stream_data else None),
            "blank_duration_secs": stream_data.get("blank_duration_secs") if stream_data.get("blank_probe_ran") else None,
            "blank_ratio": stream_data.get("blank_ratio") if stream_data.get("blank_probe_ran") else None,
            "freeze_probe_ran": True if stream_data.get("freeze_probe_ran") else (False if "freeze_probe_ran" in stream_data else None),
            "freeze_detected": stream_data.get("freeze_detected") if stream_data.get("freeze_probe_ran") else (False if "freeze_detected" in stream_data else None),
            "freeze_duration_secs": stream_data.get("freeze_duration_secs") if stream_data.get("freeze_probe_ran") else None,
            "freeze_ratio": stream_data.get("freeze_ratio") if stream_data.get("freeze_probe_ran") else None,
        }
        
        # Clean up the payload, removing None and N/A values.
        # PRESERVE_FALSE: keep False values for boolean loop fields so they
        # explicitly clear stale True values in Dispatcharr on PATCH merge.
        PRESERVE_FALSE = {
            "loop_probe_ran",
            "loop_detected",
            "blank_probe_ran",
            "blank_detected",
            "freeze_probe_ran",
            "freeze_detected",
            "visual_probe_ran",
            "visual_probe_completed",
            "visual_probe_incomplete",
            "visual_probe_duration_adjusted",
            "measurement_incomplete",
            "bitrate_recheck_required",
            "bitrate_recheck_attempted",
        }
        PRESERVE_NULL = set()
        if not preserve_existing_bitrate:
            PRESERVE_NULL.add("ffmpeg_output_bitrate")
        stream_stats_payload = {
            k: v for k, v in stream_stats_payload.items()
            if v not in [None, "N/A"] or (v is None and k in PRESERVE_NULL)
        }
        for k in PRESERVE_FALSE:
            if k in stream_stats_payload or stream_data.get(k) is False:
                stream_stats_payload[k] = stream_data.get(k) if stream_data.get(k) is not None else False
        
        if not stream_stats_payload:
            logger.debug(f"No data to update for stream {stream_id}. Skipping.")
            return False
        
        successful, failed = batch_update_stream_stats([
            {'stream_id': stream_id, 'stream_stats': stream_stats_payload}
        ], batch_size=1)
        return successful == 1 and failed == 0

    def _prepare_stream_stats_for_batch(self, stream_data: Dict) -> Optional[Dict[str, Any]]:
        """
        Prepare stream stats for batch update.
        
        This method extracts and formats stream stats from analyzed stream data
        for use in batch update operations.
        
        Parameters:
            stream_data (Dict): Analyzed stream data with resolution, fps, codecs, bitrate
            
        Returns:
            Optional[Dict[str, Any]]: Dict with 'stream_id' and 'stream_stats' keys,
                                     or None if no valid stats to update
        """
        stream_id = stream_data.get("stream_id")
        if not stream_id:
            logger.warning("No stream_id in stream data. Skipping stats preparation.")
            return None
        
        bitrate_value = self._bitrate_payload_value(stream_data.get("bitrate_kbps"))
        preserve_existing_bitrate = self._should_preserve_existing_bitrate(stream_data)

        # Construct the stream stats payload from the analyzed stream data
        stream_stats_payload = {
            "resolution": stream_data.get("resolution"),
            "source_fps": stream_data.get("fps"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
            "hdr_format": stream_data.get("hdr_format"),
            "pixel_format": stream_data.get("pixel_format"),
            "audio_sample_rate": stream_data.get("audio_sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
            "channel_layout": stream_data.get("channel_layout"),
            "audio_bitrate": stream_data.get("audio_bitrate"),
            "ffmpeg_output_bitrate": bitrate_value,
            "bitrate_source": stream_data.get("bitrate_source"),
            "quality_score": stream_data.get("score"),
            "quality_reason": stream_data.get("quality_reason"),
            "quality_reason_detail": stream_data.get("quality_reason_detail"),
            "quality_reason_context": stream_data.get("quality_reason_context"),
            "measurement_incomplete": bool(stream_data.get("measurement_incomplete")),
            "measurement_incomplete_reason": stream_data.get("measurement_incomplete_reason") or "none",
            "measurement_incomplete_context": stream_data.get("measurement_incomplete_context") or {},
            "bitrate_recheck_required": bool(stream_data.get("bitrate_recheck_required")),
            "bitrate_recheck_attempted": bool(stream_data.get("bitrate_recheck_attempted")),
            "bitrate_recheck_outcome": stream_data.get("bitrate_recheck_outcome") or "not_needed",
            "visual_probe_ran": True if stream_data.get("visual_probe_ran") else False,
            "visual_probe_completed": stream_data.get("visual_probe_completed") if "visual_probe_completed" in stream_data else False,
            "visual_probe_incomplete": stream_data.get("visual_probe_incomplete") if "visual_probe_incomplete" in stream_data else False,
            "visual_probe_incomplete_reason": (
                stream_data.get("visual_probe_incomplete_reason") or "none"
                if "visual_probe_ran" in stream_data or "visual_probe_incomplete_reason" in stream_data
                else None
            ),
            "visual_probe_requested_duration_seconds": stream_data.get(
                "visual_probe_requested_duration_seconds"
            ),
            "visual_probe_minimum_duration_seconds": stream_data.get(
                "visual_probe_minimum_duration_seconds"
            ),
            "visual_probe_duration_seconds": stream_data.get("visual_probe_duration_seconds"),
            "visual_probe_duration_adjusted": stream_data.get("visual_probe_duration_adjusted"),
            "visual_probe_duration_adjustment_reason": stream_data.get(
                "visual_probe_duration_adjustment_reason"
            ),
            "visual_probe_elapsed_time": stream_data.get("visual_probe_elapsed_time"),
            # PRESERVE_FALSE: emit False (not None) so the None-filter below keeps these
            # fields in the payload even when the probe ran but found no loop.
            # Without this, Dispatcharr's PATCH merge leaves a stale loop_probe_ran: true
            # from a previous run in place for streams not probed in the current run.
            "loop_probe_ran": True if stream_data.get("loop_probe_ran") else False,
            "loop_detected": stream_data.get("loop_detected") if stream_data.get("loop_probe_ran") else False,
            "loop_duration_secs": stream_data.get("loop_duration_secs") if stream_data.get("loop_detected") else None,
            "loop_score_penalty": stream_data.get("loop_score_penalty"),
            "blank_probe_ran": True if stream_data.get("blank_probe_ran") else False,
            "blank_detected": stream_data.get("blank_detected") if stream_data.get("blank_probe_ran") else False,
            "blank_duration_secs": stream_data.get("blank_duration_secs") if stream_data.get("blank_probe_ran") else None,
            "blank_ratio": stream_data.get("blank_ratio") if stream_data.get("blank_probe_ran") else None,
            "freeze_probe_ran": True if stream_data.get("freeze_probe_ran") else False,
            "freeze_detected": stream_data.get("freeze_detected") if stream_data.get("freeze_probe_ran") else False,
            "freeze_duration_secs": stream_data.get("freeze_duration_secs") if stream_data.get("freeze_probe_ran") else None,
            "freeze_ratio": stream_data.get("freeze_ratio") if stream_data.get("freeze_probe_ran") else None,
        }
        
        # Clean up the payload, removing None and N/A values.
        # PRESERVE_FALSE: keep False values for boolean loop fields so they
        # explicitly clear stale True values in Dispatcharr on PATCH merge.
        PRESERVE_FALSE = {
            "loop_probe_ran",
            "loop_detected",
            "blank_probe_ran",
            "blank_detected",
            "freeze_probe_ran",
            "freeze_detected",
            "visual_probe_ran",
            "visual_probe_completed",
            "visual_probe_incomplete",
            "visual_probe_duration_adjusted",
            "measurement_incomplete",
            "bitrate_recheck_required",
            "bitrate_recheck_attempted",
        }
        QUALITY_FIELDS = {
            "quality_reason",
            "quality_reason_detail",
            "quality_reason_context",
        }
        stream_stats_payload = {
            k: v for k, v in stream_stats_payload.items()
            if (
                v not in [None, "N/A"]
                or (
                    v is None
                    and k not in PRESERVE_FALSE
                    and k not in QUALITY_FIELDS
                    and not (k == "ffmpeg_output_bitrate" and preserve_existing_bitrate)
                )
            )
        }
        for k in PRESERVE_FALSE:
            if k in stream_stats_payload or stream_data.get(k) is False:
                stream_stats_payload[k] = stream_data.get(k) if stream_data.get(k) is not None else False
        
        if not stream_stats_payload:
            logger.debug(f"No data to update for stream {stream_id}. Skipping.")
            return None
        
        return {
            'stream_id': stream_id,
            'stream_stats': stream_stats_payload
        }

