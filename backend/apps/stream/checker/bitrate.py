"""Bitrate responsibilities for the shared StreamCheckerService instance."""

import logging
import json
import threading
from typing import Any, Callable, Dict, List, Optional
from apps.core.log_sanitizer import audit_ref as _audit_ref
from apps.stream.quality_report_fields import BITRATE_RECHECK_REPORT_FIELDS
from apps.core.stream_stats_utils import parse_bitrate_value

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerBitrateMixin:
    @staticmethod
    def _bitrate_payload_value(value: Any) -> Optional[int]:
        parsed = parse_bitrate_value(value)
        if parsed is None or parsed <= 0:
            return None
        return int(parsed)


    @staticmethod
    def _load_stream_stats_blob(stream_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(stream_data, dict):
            return {}
        stream_stats = stream_data.get("stream_stats") or {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats) or {}
            except json.JSONDecodeError:
                stream_stats = {}
        return stream_stats if isinstance(stream_stats, dict) else {}


    @classmethod
    def _previous_stream_bitrate(cls, stream_data: Optional[Dict[str, Any]]) -> Optional[int]:
        stream_stats = cls._load_stream_stats_blob(stream_data)
        return cls._bitrate_payload_value(
            stream_stats.get("ffmpeg_output_bitrate")
            or stream_stats.get("bitrate_kbps")
            or stream_stats.get("bitrate")
        )


    @classmethod
    def _should_preserve_existing_bitrate(cls, stream_data: Dict[str, Any]) -> bool:
        if cls._bitrate_payload_value(stream_data.get("bitrate_kbps")) is not None:
            return False
        return bool(
            stream_data.get("measurement_incomplete")
            or stream_data.get("bitrate_recheck_required")
        )


    @classmethod
    def _apply_previous_bitrate_fallback(
        cls,
        analyzed: Dict[str, Any],
        existing_stream: Optional[Dict[str, Any]],
    ) -> None:
        if not cls._should_preserve_existing_bitrate(analyzed):
            return
        previous_bitrate = cls._previous_stream_bitrate(existing_stream)
        if previous_bitrate is None:
            return
        analyzed["scoring_bitrate_kbps"] = previous_bitrate
        analyzed["bitrate_preserved_from_previous_measurement"] = True
        analyzed["preserved_bitrate_kbps"] = previous_bitrate
        analyzed["preserved_bitrate_source"] = "previous_stream_stats"
        context = analyzed.get("measurement_incomplete_context")
        if not isinstance(context, dict):
            context = {}
        context.setdefault("preserved_bitrate_kbps", previous_bitrate)
        context.setdefault("preserved_bitrate_source", "previous_stream_stats")
        analyzed["measurement_incomplete_context"] = context


    @staticmethod
    def _current_probe_stats_source(
        stream_data: Optional[Dict[str, Any]],
        analyzed: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return current probe stats when present, avoiding stale cache display."""
        if isinstance(analyzed, dict):
            return analyzed
        return stream_data if isinstance(stream_data, dict) else {}


    @classmethod
    def _has_incomplete_bitrate_measurement(cls, stream_data: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(stream_data, dict):
            return False
        if cls._bitrate_payload_value(stream_data.get("bitrate_kbps")) is not None:
            return False
        return bool(
            stream_data.get("measurement_incomplete_reason") in {
                "missing_bitrate",
                "missing_bitrate_after_recheck",
            }
            or stream_data.get("bitrate_recheck_required")
        )


    @classmethod
    def _apply_incomplete_bitrate_status(
        cls,
        target: Dict[str, Any],
        source: Optional[Dict[str, Any]],
    ) -> None:
        if not cls._has_incomplete_bitrate_measurement(source):
            return
        context = {}
        if isinstance(source, dict) and isinstance(source.get("measurement_incomplete_context"), dict):
            context = source.get("measurement_incomplete_context") or {}
        reason = "missing_bitrate"
        if isinstance(source, dict) and source.get("measurement_incomplete_reason") in {
            "missing_bitrate",
            "missing_bitrate_after_recheck",
        }:
            reason = source.get("measurement_incomplete_reason")
        target["status"] = "incomplete_bitrate"
        target["reason"] = reason
        target["reason_detail"] = reason
        target["quality_reason"] = reason
        target["quality_reason_detail"] = reason
        target["quality_reason_context"] = context
        target["measurement_incomplete"] = True
        target["measurement_incomplete_reason"] = reason
        target["measurement_incomplete_context"] = context
        target["bitrate_recheck_required"] = True
        if isinstance(source, dict):
            target["bitrate_recheck_attempted"] = bool(
                source.get("bitrate_recheck_attempted")
            )
            target["bitrate_recheck_outcome"] = (
                source.get("bitrate_recheck_outcome") or "not_needed"
            )


    @staticmethod
    def _copy_bitrate_recheck_report_fields(
        target: Dict[str, Any],
        source: Optional[Dict[str, Any]],
    ) -> None:
        """Keep final bitrate-recheck evidence in live and Changelog rows."""
        if not isinstance(source, dict):
            return
        for field in BITRATE_RECHECK_REPORT_FIELDS:
            if field in source:
                target[field] = source.get(field)


    @classmethod
    def _merge_deferred_bitrate_recheck(
        cls,
        initial: Dict[str, Any],
        recheck: Optional[Dict[str, Any]],
    ) -> str:
        """Merge only bitrate evidence from a deferred basis probe.

        The initial result remains authoritative for media identity and visual
        detections. A lightweight bitrate recheck must never erase a valid
        blank/freeze result or turn a playable stream into an offline stream.
        """
        if not isinstance(recheck, dict):
            initial["bitrate_recheck_attempted"] = False
            initial["bitrate_recheck_outcome"] = "not_run"
            return "not_run"

        if recheck.get("provider_limit_skipped"):
            initial["bitrate_recheck_attempted"] = False
            initial["bitrate_recheck_outcome"] = "provider_capacity_unavailable"
            context = initial.get("measurement_incomplete_context")
            if not isinstance(context, dict):
                context = {}
            context["bitrate_recheck_outcome"] = "provider_capacity_unavailable"
            context["bitrate_recheck_reason"] = (
                recheck.get("skipped_reason")
                or recheck.get("reason_detail")
                or "provider_capacity_unavailable"
            )
            initial["measurement_incomplete_context"] = context
            return "provider_capacity_unavailable"

        initial["bitrate_recheck_attempted"] = True
        bitrate = cls._bitrate_payload_value(recheck.get("bitrate_kbps"))
        recheck_status = str(recheck.get("status") or "unknown")

        if bitrate is not None and recheck_status.lower() == "ok":
            initial["bitrate_kbps"] = recheck.get("bitrate_kbps")
            initial["bitrate_source"] = recheck.get("bitrate_source")
            initial["measurement_incomplete"] = False
            initial["measurement_incomplete_reason"] = "none"
            initial["measurement_incomplete_context"] = {}
            initial["bitrate_recheck_required"] = False
            initial["bitrate_recheck_outcome"] = "recovered"
            initial["quality_reason"] = "none"
            initial["quality_reason_detail"] = "none"
            initial["quality_reason_context"] = {}
            for key in (
                "scoring_bitrate_kbps",
                "bitrate_preserved_from_previous_measurement",
                "preserved_bitrate_kbps",
                "preserved_bitrate_source",
            ):
                initial.pop(key, None)
            return "recovered"

        context = initial.get("measurement_incomplete_context")
        if not isinstance(context, dict):
            context = {}
        context.update({
            key: value
            for key, value in {
                "bitrate_recheck_outcome": "unavailable",
                "bitrate_recheck_status": recheck_status,
                "bitrate_recheck_source": recheck.get("bitrate_source"),
                "bitrate_recheck_elapsed_seconds": recheck.get("elapsed_time"),
                "bitrate_recheck_ffprobe_fallback_reason": recheck.get(
                    "ffprobe_fallback_reason"
                ),
            }.items()
            if value not in (None, "", [], {})
        })
        initial["measurement_incomplete"] = True
        initial["measurement_incomplete_reason"] = "missing_bitrate_after_recheck"
        initial["measurement_incomplete_context"] = context
        initial["bitrate_recheck_required"] = True
        initial["bitrate_recheck_outcome"] = "unavailable"
        initial["quality_reason"] = "missing_bitrate_after_recheck"
        initial["quality_reason_detail"] = "missing_bitrate_after_recheck"
        initial["quality_reason_context"] = context
        return "unavailable"


    def _run_deferred_bitrate_rechecks(
        self,
        results: List[Dict[str, Any]],
        streams_by_id: Dict[Any, Dict[str, Any]],
        recheck_stream: Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]],
        *,
        abort_event: Optional[threading.Event] = None,
        on_start: Optional[Callable[[Dict[str, Any], int, int], None]] = None,
        on_complete: Optional[
            Callable[[Dict[str, Any], str, int, int], None]
        ] = None,
    ) -> List[Dict[str, Any]]:
        """Recheck missing bitrates one at a time after initial channel analysis."""
        if not self._is_bitrate_recheck_enabled():
            return results

        result_by_id = {
            str(result.get("stream_id")): result
            for result in results
            if isinstance(result, dict) and result.get("stream_id") is not None
        }
        candidates = []
        for stream_id, stream in streams_by_id.items():
            initial = result_by_id.get(str(stream_id))
            initial_status = str((initial or {}).get("status") or "").lower()
            if (
                initial is not None
                and not initial.get("provider_limit_skipped")
                # Blank/freeze are successful basis probes with a later visual
                # classification. They still require the deferred bitrate pass
                # when that successful basis probe could not measure bitrate.
                and initial_status in {"ok", "blank", "freeze"}
                and initial.get("bitrate_recheck_required") is True
                and self._has_incomplete_bitrate_measurement(initial)
            ):
                candidates.append((stream, initial))

        if not candidates:
            return results

        logger.info(
            "Starting %s deferred bitrate recheck(s) sequentially after the "
            "initial channel scan",
            len(candidates),
        )
        total = len(candidates)
        for index, (stream, initial) in enumerate(candidates, 1):
            if abort_event is not None and abort_event.is_set():
                logger.info("Abort requested while running deferred bitrate rechecks")
                break
            if on_start:
                on_start(initial, index, total)
            try:
                recheck = recheck_stream(stream, initial)
            except Exception as exc:
                logger.warning(
                    "Deferred bitrate recheck raised %s for stream_ref=%s",
                    type(exc).__name__,
                    _audit_ref("stream", initial.get("stream_id")),
                )
                recheck = {
                    "status": "Error",
                    "bitrate_kbps": None,
                    "elapsed_time": 0,
                }
            if abort_event is not None and abort_event.is_set():
                logger.info(
                    "Abort requested after deferred bitrate probe for stream_ref=%s; "
                    "discarding its outcome",
                    _audit_ref("stream", initial.get("stream_id")),
                )
                break
            outcome = self._merge_deferred_bitrate_recheck(initial, recheck)
            logger.info(
                "Deferred bitrate recheck %s/%s finished for stream_ref=%s: %s",
                index,
                total,
                _audit_ref("stream", initial.get("stream_id")),
                outcome,
            )
            if on_complete:
                on_complete(initial, outcome, index, total)

        return results


    def _is_bitrate_recheck_enabled(self) -> bool:
        """Return whether deferred missing-bitrate rechecks are enabled."""
        config = getattr(self, "config", None)
        if config is None:
            return True

        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                return bool(getter("stream_analysis.bitrate_recheck_enabled", True))
            except Exception:
                return True

        if isinstance(config, dict):
            stream_analysis = config.get("stream_analysis")
            if isinstance(stream_analysis, dict):
                return bool(stream_analysis.get("bitrate_recheck_enabled", True))
        return True

