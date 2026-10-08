"""Changelog responsibilities for the shared StreamCheckerService instance."""

import logging
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerChangelogMixin:
    def _start_batch_changelog(
        self,
        *,
        require_not_aborted: bool = False,
    ) -> Optional[int]:
        """Start a new batch for changelog entries.

        Queue workers use ``require_not_aborted`` to linearize the narrow
        interval between popping an entry and claiming its changelog batch.
        ``clear_queue`` publishes the abort before taking ``batch_lock``; the
        clear therefore either discards a batch started first or prevents a
        cleared entry from starting one afterward.
        """
        with self.batch_lock:
            if require_not_aborted and self.abort_current_check.is_set():
                return None

            active_generation = getattr(
                self,
                '_active_batch_changelog_generation',
                None,
            )
            if self.batch_start_time is not None:
                # Backward-compatible recovery for tests or restored objects
                # which predate generation tracking but already own a batch.
                if active_generation is None:
                    self._batch_changelog_generation = (
                        int(getattr(self, '_batch_changelog_generation', 0)) + 1
                    )
                    active_generation = self._batch_changelog_generation
                    self._active_batch_changelog_generation = active_generation
                return active_generation

            self._batch_changelog_generation = (
                int(getattr(self, '_batch_changelog_generation', 0)) + 1
            )
            active_generation = self._batch_changelog_generation
            self._active_batch_changelog_generation = active_generation
            self.batch_start_time = datetime.now().isoformat()
            self.batch_changelog_entries = []
            logger.debug(
                "Started changelog batch generation %s",
                active_generation,
            )
            return active_generation


    def _add_to_batch_changelog(
        self,
        channel_entry: Dict[str, Any],
        *,
        batch_generation: Optional[int] = None,
    ) -> bool:
        """Add a channel check result to the current batch.

        Args:
            channel_entry: Dictionary containing channel check results
            batch_generation: Optional generation token returned by
                ``_start_batch_changelog``. Queue-worker calls always provide
                it; omission remains supported for direct legacy callers.
        """
        with self.batch_lock:
            active_generation = getattr(
                self,
                '_active_batch_changelog_generation',
                None,
            )
            if (
                batch_generation is not None
                and batch_generation != active_generation
            ):
                logger.debug(
                    "Ignoring stale changelog add for batch generation %s "
                    "(active: %s)",
                    batch_generation,
                    active_generation,
                )
                return False
            if self.batch_start_time is not None:
                self.batch_changelog_entries.append(channel_entry)
                logger.debug(f"Added channel entry to batch (total: {len(self.batch_changelog_entries)})")
                return True
            return False


    def _build_batch_changelog_entry(
        self,
        *,
        channel_id: int,
        channel_name: str,
        logo_url: Optional[str],
        total_streams: int,
        stream_stats: List[Dict[str, Any]],
        averages: Dict[str, Any],
        skipped_streams: Optional[List[Dict[str, Any]]] = None,
        channel_visibility: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the canonical, complete per-channel batch changelog payload."""
        complete_stats = deepcopy([
            item for item in stream_stats
            if isinstance(item, dict)
        ])
        checked = {"checked_streams": complete_stats}
        entry = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "logo_url": logo_url,
            "total_streams": total_streams,
            "streams_analyzed": len(complete_stats),
            "dead_streams_detected": self._count_failed_checked_streams(checked),
            "blank_streams_detected": self._count_checked_stream_status(checked, "blank"),
            "freeze_streams_detected": self._count_checked_stream_status(checked, "freeze"),
            "streams_revived": sum(
                1 for item in complete_stats if item.get("status") == "revived"
            ),
            "incomplete_bitrate_streams": sum(
                1 for item in complete_stats if item.get("status") == "incomplete_bitrate"
            ),
            "avg_resolution": averages.get("avg_resolution", "N/A"),
            "avg_bitrate": averages.get("avg_bitrate", "N/A"),
            "avg_fps": averages.get("avg_fps", "N/A"),
            "success": True,
            "stream_stats": complete_stats,
            "skipped_streams": deepcopy(skipped_streams or []),
        }
        visibility_changelog = self._visibility_changelog_result(channel_visibility)
        if visibility_changelog:
            entry["channel_visibility"] = visibility_changelog
        return entry


    def _finalize_batch_changelog(
        self,
        *,
        batch_generation: Optional[int] = None,
    ) -> bool:
        """Finalize the current batch and create a consolidated changelog entry."""
        with self.batch_lock:
            active_generation = getattr(
                self,
                '_active_batch_changelog_generation',
                None,
            )
            if (
                batch_generation is not None
                and batch_generation != active_generation
            ):
                logger.debug(
                    "Ignoring stale changelog finalizer for batch generation %s "
                    "(active: %s)",
                    batch_generation,
                    active_generation,
                )
                return False
            if self.batch_start_time is None or len(self.batch_changelog_entries) == 0:
                logger.debug("No batch to finalize")
                # A started batch can be left empty when its active queue entry
                # is aborted before producing a changelog row. Normalize both
                # fields here so the next queue batch always gets a fresh start
                # timestamp instead of inheriting this stale lifecycle state.
                self.batch_start_time = None
                self.batch_changelog_entries = []
                self._active_batch_changelog_generation = None
                return False

            if not self.changelog:
                logger.debug("Changelog not available, skipping batch finalization")
                self.batch_start_time = None
                self.batch_changelog_entries = []
                self._active_batch_changelog_generation = None
                return False

            try:
                # Calculate duration
                start_dt = datetime.fromisoformat(self.batch_start_time)
                end_dt = datetime.now()
                duration_seconds = int((end_dt - start_dt).total_seconds())

                # Format duration as human-readable string
                if duration_seconds < 60:
                    duration_str = f"{duration_seconds}s"
                elif duration_seconds < 3600:
                    minutes = duration_seconds // 60
                    seconds = duration_seconds % 60
                    duration_str = f"{minutes}m {seconds}s"
                else:
                    hours = duration_seconds // 3600
                    minutes = (duration_seconds % 3600) // 60
                    duration_str = f"{hours}h {minutes}m"

                # Calculate aggregate stats
                total_channels = len(self.batch_changelog_entries)
                total_streams = sum(entry.get('total_streams', 0) for entry in self.batch_changelog_entries)
                streams_analyzed = sum(entry.get('streams_analyzed', 0) for entry in self.batch_changelog_entries)
                dead_streams = sum(entry.get('dead_streams_detected', 0) for entry in self.batch_changelog_entries)
                blank_streams = sum(entry.get('blank_streams_detected', 0) for entry in self.batch_changelog_entries)
                freeze_streams = sum(entry.get('freeze_streams_detected', 0) for entry in self.batch_changelog_entries)
                streams_revived = sum(entry.get('streams_revived', 0) for entry in self.batch_changelog_entries)
                incomplete_bitrate_streams = sum(
                    entry.get('incomplete_bitrate_streams', 0)
                    for entry in self.batch_changelog_entries
                )
                successful_checks = sum(1 for entry in self.batch_changelog_entries if entry.get('success', False))
                failed_checks = total_channels - successful_checks

                # Prepare subentries in the format expected by the UI
                subentries = [{
                    "group": "check",
                    "items": [
                        {
                            "channel_id": entry.get('channel_id'),
                            "channel_name": entry.get('channel_name'),
                            "logo_url": entry.get('logo_url'),
                            "stats": {
                                "total_streams": entry.get('total_streams', 0),
                                "streams_analyzed": entry.get('streams_analyzed', 0),
                                "dead_streams": entry.get('dead_streams_detected', 0),
                                "blank_streams": entry.get('blank_streams_detected', 0),
                                "freeze_streams": entry.get('freeze_streams_detected', 0),
                                "streams_revived": entry.get('streams_revived', 0),
                                "incomplete_bitrate_streams": entry.get('incomplete_bitrate_streams', 0),
                                "avg_resolution": entry.get('avg_resolution', 'N/A'),
                                "avg_bitrate": entry.get('avg_bitrate', 'N/A'),
                                "avg_fps": entry.get('avg_fps', 'N/A'),
                                "stream_details": entry.get('stream_stats', []),
                                "skipped_streams": entry.get('skipped_streams', []),
                            }
                        }
                        for entry in self.batch_changelog_entries
                    ]
                }]

                # Create consolidated changelog entry
                self.changelog.add_entry(
                    action='batch_stream_check',
                    details={
                        'total_channels': total_channels,
                        'successful_checks': successful_checks,
                        'failed_checks': failed_checks,
                        'total_streams': total_streams,
                        'streams_analyzed': streams_analyzed,
                        'dead_streams': dead_streams,
                        'blank_streams': blank_streams,
                        'freeze_streams': freeze_streams,
                        'streams_revived': streams_revived,
                        'incomplete_bitrate_streams': incomplete_bitrate_streams,
                        'duration': duration_str,
                        'duration_seconds': duration_seconds
                    },
                    timestamp=self.batch_start_time,
                    subentries=subentries
                )

                logger.info(f"Finalized batch changelog: {total_channels} channels, {streams_analyzed} streams analyzed in {duration_str}")

                # Note: trigger_channel_re_enabling and trigger_empty_channel_disabling
                # have been deprecated as they relied on Dispatcharr channel profiles
                # which have been removed.

                return True
            except Exception as e:
                logger.error(f"Failed to finalize batch changelog: {e}", exc_info=True)
                return False
            finally:
                # Reset batch tracking
                self.batch_start_time = None
                self.batch_changelog_entries = []
                self._active_batch_changelog_generation = None

