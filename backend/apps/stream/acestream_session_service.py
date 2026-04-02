"""AceStream channel-session management and quality scoring utilities."""

import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict

import requests

from apps.core.logging_config import setup_logging
from apps.core.stream_stats_utils import extract_stream_stats
from apps.stream.acestream_monitoring_client import normalize_content_id
from apps.udi import get_udi_manager
from apps.stream.zero_decode_loop_detector import ZeroDecodeLoopDetector

logger = setup_logging(__name__)

# Global registry for active zero-decode loop detectors
_loop_detectors: Dict[str, ZeroDecodeLoopDetector] = {}

def stop_ace_loop_detector(monitor_id: str):
    if not monitor_id:
        return
    detector = _loop_detectors.pop(monitor_id, None)
    if detector:
        try:
            detector.stop()
        except Exception as e:
            logger.error(f"Error stopping loop detector for monitor {monitor_id}: {e}")


def get_ace_management_settings() -> Dict[str, float]:
    """Load management tuning from shared session settings."""
    from apps.database.manager import get_db_manager

    db = get_db_manager()
    session_settings = db.get_system_setting("session_settings", {})
    if not isinstance(session_settings, dict):
        session_settings = {}

    review_duration = float(session_settings.get("review_duration", 60.0) or 60.0)
    pass_score_threshold = 70.0
    return {
        "review_duration": max(0.0, review_duration),
        "pass_score_threshold": pass_score_threshold,
    }


def _clamp_score(value: Any) -> float:
    return max(0.0, min(100.0, float(value)))


def _coerce_float(value: Any):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _parse_resolution_pixels(resolution: Any) -> int:
    if not isinstance(resolution, str):
        return 0
    match = re.match(r"^(\d{2,5})x(\d{2,5})$", resolution.strip())
    if not match:
        return 0
    return int(match.group(1)) * int(match.group(2))


def _parse_resolution_parts(resolution: Any):
    if not isinstance(resolution, str):
        return None, None
    match = re.match(r"^(\d{2,5})x(\d{2,5})$", resolution.strip())
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _score_ffprobe_quality(ffprobe_stats: Any):
    """Compute quality bonus from cached FFProbe-like stream stats."""
    if not isinstance(ffprobe_stats, dict):
        return 0.0, "unknown"

    bonus = 0.0
    resolution = ffprobe_stats.get("resolution")
    pixels = _parse_resolution_pixels(resolution)
    if pixels >= 1920 * 1080:
        bonus += 8.0
    elif pixels >= 1280 * 720:
        bonus += 5.0
    elif pixels >= 960 * 540:
        bonus += 2.0
    elif pixels > 0:
        bonus -= 2.0

    video_codec = str(ffprobe_stats.get("video_codec") or "").lower()
    if video_codec in ("hevc", "h265", "av1"):
        bonus += 3.0
    elif video_codec in ("h264", "avc"):
        bonus += 2.0
    elif video_codec and video_codec != "n/a":
        bonus += 1.0

    audio_codec = str(ffprobe_stats.get("audio_codec") or "").lower()
    if audio_codec and audio_codec != "n/a":
        bonus += 1.0

    fps = _coerce_float(ffprobe_stats.get("fps"))
    if fps is not None:
        if fps >= 50.0:
            bonus += 2.0
        elif fps >= 23.0:
            bonus += 1.0

    bitrate_kbps = _coerce_float(ffprobe_stats.get("bitrate_kbps"))
    if bitrate_kbps is not None:
        if bitrate_kbps >= 4000.0:
            bonus += 2.0
        elif bitrate_kbps >= 1500.0:
            bonus += 1.0

    if ffprobe_stats.get("hdr_format"):
        bonus += 2.0

    if bonus >= 14.0:
        rating = "excellent"
    elif bonus >= 9.0:
        rating = "good"
    elif bonus >= 4.0:
        rating = "fair"
    else:
        rating = "basic"

    return bonus, rating


def _extract_last_ts_values(monitor: Any):
    values = []
    if not isinstance(monitor, dict):
        return values

    recent_status = monitor.get("recent_status")
    if isinstance(recent_status, list):
        for item in recent_status:
            if not isinstance(item, dict):
                continue
            last_ts = _coerce_float(item.get("last_ts"))
            if last_ts is not None:
                values.append(last_ts)

    latest = monitor.get("latest_status")
    if isinstance(latest, dict):
        latest_last_ts = _coerce_float(latest.get("last_ts"))
        if latest_last_ts is not None and (not values or values[-1] != latest_last_ts):
            values.append(latest_last_ts)

    return values


def _compute_last_ts_plateau_penalty(monitor: Any):
    """Penalize frequent last_ts plateaus (limited progression between samples)."""
    values = _extract_last_ts_values(monitor)
    if len(values) < 3:
        return 0.0, 0.0

    deltas = []
    for i in range(1, len(values)):
        deltas.append(values[i] - values[i - 1])

    if not deltas:
        return 0.0, 0.0

    plateau_steps = sum(1 for d in deltas if d <= 0.0)
    plateau_ratio = float(plateau_steps) / float(len(deltas))
    penalty = min(18.0, plateau_ratio * 18.0)

    longest = 0
    current = 0
    for d in deltas:
        if d <= 0.0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    if longest >= 3:
        penalty = min(22.0, penalty + 4.0)

    return penalty, plateau_ratio


def _compute_ace_management_score(monitor: Any, entry: Any = None):
    """Compute Ace reliability score using status, plateau behavior, and FFProbe quality."""
    if not isinstance(monitor, dict):
        return 25.0, {
            "plateau_penalty": 0.0,
            "plateau_ratio": 0.0,
            "ffprobe_bonus": 0.0,
            "ffprobe_rating": "unknown",
        }

    status = (monitor.get("status") or "").lower()
    base = {
        "running": 82.0,
        "starting": 55.0,
        "reconnecting": 50.0,
        "stuck": 25.0,
        "dead": 0.0,
    }.get(status, 40.0)

    latest = monitor.get("latest_status") or {}

    speed_down = float(latest.get("speed_down") or 0.0)
    speed_up = float(latest.get("speed_up") or 0.0)
    peers = float(latest.get("peers") or 0.0)

    base += min(8.0, speed_down / 600.0)
    base += min(4.0, speed_up / 900.0)
    base += min(6.0, peers / 8.0)

    if monitor.get("currently_played"):
        base += 3.0

    plateau_penalty, plateau_ratio = _compute_last_ts_plateau_penalty(monitor)
    ffprobe_stats = entry.get("ffprobe_stats") if isinstance(entry, dict) else None
    ffprobe_bonus, ffprobe_rating = _score_ffprobe_quality(ffprobe_stats)

    score = _clamp_score(base - plateau_penalty + ffprobe_bonus)
    return score, {
        "plateau_penalty": plateau_penalty,
        "plateau_ratio": plateau_ratio,
        "ffprobe_bonus": ffprobe_bonus,
        "ffprobe_rating": ffprobe_rating,
    }


def _evaluate_ace_entry_management(entry: Dict[str, Any], monitor: Any, now_ts: float, settings: Dict[str, Any]):
    """Derive and persist management state fields for a channel-session entry."""
    changed = False

    prev_state = (entry.get("management_state") or "review").lower()
    prev_since = float(entry.get("management_since") or now_ts)
    manual_quarantine = bool(entry.get("manual_quarantine"))

    score, score_meta = _compute_ace_management_score(monitor, entry)
    reason = None

    if not isinstance(monitor, dict):
        target_state = "review"
        reason = "missing-monitor"
    else:
        raw_status = (monitor.get("status") or "unknown").lower()
        
        # Check loop detector
        enable_loop_detection = settings.get("enable_loop_detection", False)
        if enable_loop_detection and raw_status == "running":
            monitor_id = entry.get("monitor_id")
            stream_id = entry.get("stream_id")
            content_id = entry.get("content_id")
            engine = monitor.get("engine") or {}
            host = engine.get("host")
            port = engine.get("port")
            
            if monitor_id and content_id and host and port:
                if monitor_id not in _loop_detectors:
                    url = f"http://{host}:{port}/ace/getstream?id={content_id}"
                    
                    def on_loop(loop_duration: float = 0.0):
                        logger.warning(f"Loop confirmed by detector for Acestream {stream_id}")
                        entry["is_looping"] = True
                        entry["loop_duration"] = loop_duration
                        
                    detector = ZeroDecodeLoopDetector(
                        url=url, 
                        session_id=settings.get("session_id", "unknown"),
                        stream_id=str(stream_id),
                        on_loop_detected=on_loop
                    )
                    _loop_detectors[monitor_id] = detector
                    detector.start()
                    
        # Check if loop detected and handle
        if entry.get("is_looping"):
            target_state = "quarantined"
            reason = "loop_detected"
        elif manual_quarantine:
            target_state = "quarantined"
            reason = "manual"
        elif raw_status == "dead":
            target_state = "quarantined"
            reason = "dead"
        elif raw_status == "stuck":
            target_state = "quarantined"
            reason = "stuck"
        elif raw_status in ("starting", "reconnecting"):
            target_state = "review"
            reason = raw_status
        elif raw_status == "running":
            elapsed = max(0.0, now_ts - prev_since)
            if prev_state == "stable":
                target_state = "stable"
            elif prev_state == "review" and elapsed >= settings["review_duration"] and score >= settings["pass_score_threshold"]:
                target_state = "stable"
            else:
                target_state = "review"
                if score_meta.get("plateau_ratio", 0.0) >= 0.5:
                    reason = "plateau"
                elif prev_state != "stable":
                    reason = "warming"
                else:
                    reason = "low-score"
        else:
            target_state = "review"
            reason = "status-" + raw_status
            
        if target_state != "review" and target_state != "stable" and target_state != "starting":
            # If we are quarantined or similar, we should stop the detector
            stop_ace_loop_detector(entry.get("monitor_id"))

    QUARANTINE_DURATION = 900.0
    if target_state == "quarantined" and not manual_quarantine:
        time_in_quarantine = now_ts - prev_since
        if prev_state == "quarantined" and time_in_quarantine >= QUARANTINE_DURATION:
            target_state = "review"
            reason = "auto-revive"
            logger.info(
                f"Ace stream {entry.get('stream_id')} auto-revived after {time_in_quarantine:.0f}s in quarantine"
            )

    if target_state != prev_state:
        entry["management_state"] = target_state
        entry["management_since"] = now_ts
        changed = True
    elif "management_state" not in entry:
        entry["management_state"] = target_state
        entry["management_since"] = now_ts
        changed = True

    if float(entry.get("management_score") or -1) != float(score):
        entry["management_score"] = score
        changed = True

    plateau_ratio = float(score_meta.get("plateau_ratio") or 0.0)
    if float(entry.get("management_plateau_ratio") or -1.0) != plateau_ratio:
        entry["management_plateau_ratio"] = plateau_ratio
        changed = True

    ffprobe_rating = score_meta.get("ffprobe_rating")
    if entry.get("management_ffprobe_rating") != ffprobe_rating:
        entry["management_ffprobe_rating"] = ffprobe_rating
        changed = True

    ffprobe_bonus = float(score_meta.get("ffprobe_bonus") or 0.0)
    if float(entry.get("management_ffprobe_bonus") or -999.0) != ffprobe_bonus:
        entry["management_ffprobe_bonus"] = ffprobe_bonus
        changed = True

    if entry.get("management_reason") != reason:
        entry["management_reason"] = reason
        changed = True

    window_size = 10
    current_delta = None
    if isinstance(monitor, dict):
        movement = monitor.get("livepos_movement") or {}
        raw_delta = movement.get("last_ts_delta")
        if raw_delta is not None:
            try:
                current_delta = float(raw_delta)
            except (TypeError, ValueError):
                pass
    if current_delta is not None:
        window = entry.get("_last_ts_delta_window")
        if not isinstance(window, list):
            window = []
            entry["_last_ts_delta_window"] = window
        if len(window) >= window_size:
            del window[0]
        window.append(current_delta)
        avg_delta = sum(window) / len(window)
        if entry.get("_last_ts_delta_avg") != avg_delta:
            entry["_last_ts_delta_avg"] = avg_delta
            changed = True

    return changed


def _is_ace_ffprobe_stats_empty(ffprobe_stats: Any) -> bool:
    """Return True if ffprobe stats have no useful data (all N/A or missing)."""
    if not isinstance(ffprobe_stats, dict):
        return True
    return (
        (ffprobe_stats.get("resolution") or "N/A") in ("N/A", "0x0", "")
        and (ffprobe_stats.get("video_codec") or "N/A") == "N/A"
    )


_ACE_FFPROBE_RECHECK_THROTTLE_S = 300
_ACE_FFPROBE_PROBE_DURATION_S = 5
_ACE_FFPROBE_PROBE_TIMEOUT_S = 15


def _schedule_ace_ffprobe_recheck(entry: Dict[str, Any], monitor: Any):
    """Schedule a background ffprobe recheck on the engine HTTP URL for a running stream."""
    if not _is_ace_ffprobe_stats_empty(entry.get("ffprobe_stats")):
        return

    monitor_status = (monitor.get("status") or "").lower() if isinstance(monitor, dict) else ""
    if monitor_status != "running":
        return

    last_attempt = entry.get("_ffprobe_attempt_ts")
    if last_attempt and time.time() - float(last_attempt) < _ACE_FFPROBE_RECHECK_THROTTLE_S:
        return

    engine = (monitor.get("engine") or {}) if isinstance(monitor, dict) else {}
    host = engine.get("host")
    port = engine.get("port")
    content_id = entry.get("content_id")

    if not host or not port or not content_id:
        return

    url = f"http://{host}:{port}/ace/getstream?id={content_id}"
    entry["_ffprobe_attempt_ts"] = time.time()
    thread_name = f"ace-ffprobe-{content_id[:8] if content_id else 'unknown'}"

    def _run():
        try:
            from apps.stream.stream_check_utils import get_stream_info_and_bitrate

            stats = get_stream_info_and_bitrate(
                url,
                duration=_ACE_FFPROBE_PROBE_DURATION_S,
                timeout=_ACE_FFPROBE_PROBE_TIMEOUT_S,
            )
            if stats and (
                stats.get("resolution", "N/A") not in ("N/A", "0x0", "")
                or stats.get("video_codec", "N/A") != "N/A"
            ):
                entry["ffprobe_stats"] = {
                    "resolution": stats.get("resolution"),
                    "fps": stats.get("fps"),
                    "bitrate_kbps": stats.get("bitrate_kbps"),
                    "video_codec": stats.get("video_codec", "N/A"),
                    "audio_codec": stats.get("audio_codec", "N/A"),
                    "hdr_format": stats.get("hdr_format"),
                    "pixel_format": stats.get("pixel_format"),
                    "audio_sample_rate": stats.get("audio_sample_rate"),
                    "audio_channels": stats.get("audio_channels"),
                    "channel_layout": stats.get("channel_layout"),
                    "audio_bitrate": stats.get("audio_bitrate"),
                }
                logger.info(
                    f"AceStream ffprobe recheck succeeded for {content_id}: {entry['ffprobe_stats'].get('resolution')}"
                )
            else:
                logger.debug(f"AceStream ffprobe recheck yielded no useful data for {content_id}")
        except Exception as exc:
            logger.debug(f"AceStream ffprobe recheck failed for {content_id}: {exc}")

    thread = threading.Thread(target=_run, daemon=True, name=thread_name)
    thread.start()


def evaluate_ace_session_management(raw_session: Any, monitors_by_id: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    """Evaluate and persist management state for every entry in one session."""
    if not isinstance(raw_session, dict):
        return False

    changed = False
    now_ts = time.time()
    udi = get_udi_manager()
    ffprobe_cache: Dict[Any, Any] = {}
    entries = raw_session.get("entries") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        if not isinstance(entry.get("ffprobe_stats"), dict):
            stream_id = entry.get("stream_id")
            stream = None
            if stream_id not in ffprobe_cache:
                try:
                    stream = udi.get_stream_by_id(int(stream_id)) if stream_id is not None else None
                except Exception:
                    stream = None
                ffprobe_cache[stream_id] = stream
            else:
                stream = ffprobe_cache.get(stream_id)

            if isinstance(stream, dict):
                entry["ffprobe_stats"] = extract_stream_stats(stream)
                changed = True

        monitor = monitors_by_id.get(entry.get("monitor_id"))
        _schedule_ace_ffprobe_recheck(entry, monitor)
        
        # Pass session info through settings
        eval_settings = dict(settings)
        eval_settings["enable_loop_detection"] = raw_session.get("enable_loop_detection", False)
        eval_settings["session_id"] = raw_session.get("session_id", "unknown")
        
        if _evaluate_ace_entry_management(entry, monitor, now_ts, eval_settings):
            changed = True

    return changed


def check_ace_session_epg_auto_stop(raw_session: Any, client: Any) -> bool:
    """Stop orchestrator monitors for a session when its EPG event end time has passed."""
    if not isinstance(raw_session, dict):
        return False
    if raw_session.get("epg_auto_stopped"):
        return False

    epg_end = raw_session.get("epg_event_end")
    if not epg_end:
        return False

    try:
        from datetime import timezone

        end_time = datetime.fromisoformat(str(epg_end).replace("Z", "+00:00"))
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < end_time:
            return False
    except Exception:
        return False

    session_id = raw_session.get("session_id", "?")
    logger.info(f"Ace session {session_id}: EPG event ended, auto-stopping Orchestrator monitors")

    for entry in raw_session.get("entries") or []:
        monitor_id = entry.get("monitor_id") if isinstance(entry, dict) else None
        if not monitor_id:
            continue
        try:
            client.stop_session(monitor_id)
        except Exception:
            pass

    raw_session["epg_auto_stopped"] = True
    return True


def apply_ace_dispatcharr_sync(raw_session: Any) -> bool:
    """Enforce Dispatcharr channel stream order based on AceStream management states."""
    if not isinstance(raw_session, dict):
        return False

    channel_id = raw_session.get("channel_id")
    if not channel_id:
        return False

    entries = [e for e in (raw_session.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        return False

    stable = sorted(
        [e for e in entries if (e.get("management_state") or "review").lower() == "stable"],
        key=lambda e: (float(e.get("management_score") or 0), float(e.get("_last_ts_delta_avg") or 0)),
        reverse=True,
    )
    review = sorted(
        [e for e in entries if (e.get("management_state") or "review").lower() == "review"],
        key=lambda e: (float(e.get("management_score") or 0), float(e.get("_last_ts_delta_avg") or 0)),
        reverse=True,
    )

    desired_ids = [int(e["stream_id"]) for e in (stable + review) if e.get("stream_id") is not None]
    if not desired_ids:
        return False

    try:
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(int(channel_id))
        current_ids = channel.get("streams", []) if isinstance(channel, dict) else []

        if desired_ids == current_ids:
            return False

        from apps.core.api_utils import update_channel_streams

        def _do_sync():
            try:
                success = update_channel_streams(int(channel_id), desired_ids)
                if success:
                    udi.refresh_channel_by_id(int(channel_id))
                    logger.debug(
                        f"Ace session {raw_session.get('session_id')}: synced channel {channel_id} "
                        f"stream order -> {desired_ids}"
                    )
                else:
                    logger.warning(f"Ace Dispatcharr sync failed for channel {channel_id}")
            except Exception as exc:
                logger.error(f"Ace Dispatcharr sync error for channel {channel_id}: {exc}")

        thread = threading.Thread(target=_do_sync, daemon=True, name=f"AceSync-{channel_id}")
        thread.start()
        return True

    except Exception as exc:
        logger.error(f"apply_ace_dispatcharr_sync error: {exc}")
        return False


def save_ace_session_telemetry_snapshot(raw_session: Any, monitors_by_id: Dict[str, Any]) -> bool:
    """Persist Ace monitoring telemetry to DB, deduped by latest monitor sample key."""
    if not isinstance(raw_session, dict):
        return False

    entries = raw_session.get("entries") or []
    if not isinstance(entries, list) or not entries:
        return False

    pending_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        monitor = monitors_by_id.get(entry.get("monitor_id"))
        if not isinstance(monitor, dict):
            continue

        sample_key = str(monitor.get("last_collected_at") or monitor.get("sample_count") or "")
        if not sample_key:
            continue
        if entry.get("last_telemetry_key") == sample_key:
            continue

        pending_entries.append((entry, monitor, sample_key))

    if not pending_entries:
        return False

    from apps.database.connection import get_session as get_db_session
    from apps.database.models import Run, ChannelHealth, StreamTelemetry

    db_session = get_db_session()
    try:
        run_ts = datetime.utcnow()
        channel_id = int(raw_session.get("channel_id") or 0)
        channel_name = raw_session.get("channel_name")

        all_monitors = []
        quarantined_count = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            monitor = monitors_by_id.get(entry.get("monitor_id"))
            if isinstance(monitor, dict):
                all_monitors.append(monitor)
            if (entry.get("management_state") or "").lower() == "quarantined":
                quarantined_count += 1

        run = Run(
            timestamp=run_ts,
            duration_seconds=0.0,
            total_channels=1,
            total_streams=len(entries),
            global_dead_count=quarantined_count,
            global_revived_count=0,
            run_type="acestream_monitor",
            raw_details=json.dumps(
                {
                    "source_type": "acestream",
                    "session_id": raw_session.get("session_id"),
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "pending_streams": len(pending_entries),
                }
            ),
            raw_subentries=None,
        )
        db_session.add(run)
        db_session.flush()

        channel_health = ChannelHealth(
            run_id=run.id,
            channel_id=channel_id,
            channel_name=channel_name,
            offline=len(all_monitors) == 0,
            available_streams=max(0, len(entries) - quarantined_count),
            dead_streams=quarantined_count,
        )
        db_session.add(channel_health)

        udi = get_udi_manager()
        stream_cache: Dict[int, Any] = {}
        for entry, monitor, sample_key in pending_entries:
            stream_id = int(entry.get("stream_id") or 0)
            if stream_id not in stream_cache:
                stream_cache[stream_id] = udi.get_stream_by_id(stream_id) if stream_id else None
            stream = stream_cache.get(stream_id)

            ffprobe_stats = entry.get("ffprobe_stats") if isinstance(entry.get("ffprobe_stats"), dict) else {}
            width, height = _parse_resolution_parts(ffprobe_stats.get("resolution"))

            row = StreamTelemetry(
                run_id=run.id,
                channel_id=channel_id,
                provider_id=(stream or {}).get("m3u_account_id") if isinstance(stream, dict) else None,
                stream_id=stream_id,
                bitrate_kbps=int(_coerce_float(ffprobe_stats.get("bitrate_kbps")) or 0) or None,
                resolution_width=width,
                resolution_height=height,
                fps=_coerce_float(ffprobe_stats.get("fps")),
                codec=ffprobe_stats.get("video_codec"),
                audio_codec=ffprobe_stats.get("audio_codec"),
                quality_score=_coerce_float(entry.get("management_score")),
                is_dead=((entry.get("management_state") or "").lower() == "quarantined"),
                is_hdr=bool(ffprobe_stats.get("hdr_format")),
            )
            db_session.add(row)
            entry["last_telemetry_key"] = sample_key

        db_session.commit()
        return True
    except Exception as exc:
        db_session.rollback()
        logger.error(f"Error saving Ace telemetry snapshot: {exc}", exc_info=True)
        return False
    finally:
        db_session.close()


def _ace_status_buckets(monitor_items):
    active_states = {"starting", "running", "stuck", "reconnecting"}
    running = 0
    stuck = 0
    dead = 0

    for item in monitor_items:
        status = (item.get("status") or "").lower()
        if status == "running":
            running += 1
        elif status == "stuck":
            stuck += 1
            dead += 1
        elif status == "dead":
            dead += 1

    is_active = any((item.get("status") or "").lower() in active_states for item in monitor_items)
    return running, stuck, dead, is_active


def build_ace_channel_session_summary(raw_session: Dict[str, Any], monitors_by_id: Dict[str, Any]):
    monitor_items = [monitors_by_id.get(entry.get("monitor_id")) for entry in raw_session.get("entries", [])]
    monitor_items = [item for item in monitor_items if isinstance(item, dict)]

    stable = 0
    review = 0
    quarantined = 0
    for entry in raw_session.get("entries", []):
        if not isinstance(entry, dict):
            continue
        state = (entry.get("management_state") or "review").lower()
        if state == "stable":
            stable += 1
        elif state == "quarantined":
            quarantined += 1
        else:
            review += 1

    _, _, _, is_active = _ace_status_buckets(monitor_items)
    return {
        "session_id": raw_session.get("session_id"),
        "source_type": "acestream",
        "channel_id": raw_session.get("channel_id"),
        "channel_name": raw_session.get("channel_name"),
        "channel_logo_url": raw_session.get("channel_logo_url"),
        "epg_event_title": raw_session.get("epg_event_title"),
        "epg_event_description": raw_session.get("epg_event_description"),
        "epg_event_start": raw_session.get("epg_event_start"),
        "epg_event_end": raw_session.get("epg_event_end"),
        "epg_event_id": raw_session.get("epg_event_id"),
        "created_at": raw_session.get("created_at"),
        "is_active": is_active,
        "stable_count": stable,
        "review_count": review,
        "quarantined_count": quarantined,
        "stream_count": len(raw_session.get("entries", [])),
        "sample_count": sum(int(item.get("sample_count") or 0) for item in monitor_items),
        "played_count": sum(1 for item in monitor_items if item.get("currently_played")),
        "monitor_count": len(raw_session.get("entries", [])),
    }


def annotate_monitors_with_playback(client: Any, monitors_by_id: Dict[str, Any]):
    """Attach currently_played to monitor payloads using started streams endpoint."""
    if not monitors_by_id:
        return

    try:
        started_payload = client.list_started_streams()
    except Exception:
        return

    for monitor_id, monitor in list(monitors_by_id.items()):
        if not isinstance(monitor, dict):
            continue
        monitors_by_id[monitor_id] = client.annotate_with_playback(dict(monitor), started_payload)


def compact_ace_monitor_payload(monitor: Any, recent_limit: int = 8):
    """Return a compact monitor shape suitable for 1s UI polling."""
    if not isinstance(monitor, dict):
        return None

    recent_status = monitor.get("recent_status")
    if isinstance(recent_status, list):
        recent_status = recent_status[-recent_limit:]
    else:
        recent_status = []

    return {
        "monitor_id": monitor.get("monitor_id"),
        "content_id": monitor.get("content_id"),
        "stream_name": monitor.get("stream_name"),
        "status": monitor.get("status"),
        "interval_s": monitor.get("interval_s"),
        "run_seconds": monitor.get("run_seconds"),
        "started_at": monitor.get("started_at"),
        "last_collected_at": monitor.get("last_collected_at"),
        "ended_at": monitor.get("ended_at"),
        "sample_count": monitor.get("sample_count"),
        "last_error": monitor.get("last_error"),
        "dead_reason": monitor.get("dead_reason"),
        "reconnect_attempts": monitor.get("reconnect_attempts"),
        "engine": monitor.get("engine"),
        "session": monitor.get("session"),
        "latest_status": monitor.get("latest_status"),
        "recent_status": recent_status,
        "livepos_movement": monitor.get("livepos_movement"),
        "currently_played": bool(monitor.get("currently_played")),
    }


def refresh_ace_session_streams(
    raw_session: Any,
    client: Any,
    interval_s: float = 1.0,
    run_seconds: int = 0,
    per_sample_timeout_s: float = 1.0,
) -> bool:
    """Discover new streams matching the session filter and start monitors for them."""
    if not isinstance(raw_session, dict):
        return False

    channel_id = raw_session.get("channel_id")
    if not channel_id:
        return False

    resolved_regex = raw_session.get("regex_filter", "")
    match_by_tvg_id = bool(raw_session.get("match_by_tvg_id"))
    channel_tvg_id = raw_session.get("channel_tvg_id")
    channel_name = raw_session.get("channel_name", "")

    if not resolved_regex and not (match_by_tvg_id and channel_tvg_id):
        return False

    compiled_regex = None
    if resolved_regex:
        try:
            pattern = resolved_regex.replace("CHANNEL_NAME", re.escape(channel_name))
            pattern = re.sub(r"(?<!\\) +", r"\\s+", pattern)
            compiled_regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return False

    udi = get_udi_manager()
    all_streams = udi.get_streams() or []

    existing_content_ids = {
        e.get("content_id")
        for e in (raw_session.get("entries") or [])
        if isinstance(e, dict) and e.get("content_id")
    }

    new_entries = []
    for stream in all_streams:
        matched = False
        if match_by_tvg_id and channel_tvg_id:
            if stream.get("tvg_id") == channel_tvg_id:
                matched = True
        if not matched and compiled_regex:
            if compiled_regex.search((stream.get("name") or "")[:1000]):
                matched = True
        if not matched:
            continue

        content_id = normalize_content_id(stream.get("url"))
        if not content_id or content_id in existing_content_ids:
            continue

        try:
            payload = {
                "content_id": content_id,
                "stream_name": stream.get("name") or channel_name,
                "interval_s": float(interval_s),
                "run_seconds": int(run_seconds),
                "per_sample_timeout_s": float(per_sample_timeout_s),
            }
            started = client.start_session(payload)
            monitor_id = started.get("monitor_id") if isinstance(started, dict) else None
            if not monitor_id:
                continue
            new_entries.append(
                {
                    "stream_id": stream.get("id"),
                    "stream_name": stream.get("name"),
                    "content_id": content_id,
                    "monitor_id": monitor_id,
                    "ffprobe_stats": extract_stream_stats(stream),
                }
            )
            existing_content_ids.add(content_id)
            logger.info(
                f"Ace session {raw_session.get('session_id')}: discovered new stream "
                f"{stream.get('name')} ({content_id})"
            )
        except Exception as exc:
            logger.warning(f"Ace refresh: failed to start monitor for {content_id}: {exc}")

    if new_entries:
        raw_session.setdefault("entries", []).extend(new_entries)
        return True
    return False


def create_acestream_channel_session_impl(
    channel_id: Any,
    interval_s: float = 1.0,
    run_seconds: int = 0,
    per_sample_timeout_s: float = 1.0,
    engine_container_id: Any = None,
    epg_event_title: Any = None,
    epg_event_description: Any = None,
    epg_event_start: Any = None,
    epg_event_end: Any = None,
    epg_event_id: Any = None,
    enable_loop_detection: bool = False,
    *,
    get_client_or_error,
    ping_orchestrator_ready,
    load_store,
    save_store,
):
    """Core logic to create and start an AceStream channel session."""
    client, error_response = get_client_or_error()
    if error_response:
        return {"error": "AceStream backend orchestrator is not configured."}, 503

    ready, ready_detail = ping_orchestrator_ready(client)
    if not ready:
        return {
            "error": "AceStream orchestrator is not ready",
            "detail": ready_detail,
        }, 503

    try:
        udi = get_udi_manager()

        udi.refresh_channel_by_id(int(channel_id))
        channel = udi.get_channel_by_id(int(channel_id))
        if not channel:
            return {"error": "Channel not found"}, 404

        channel_name = channel.get("name") or f"Channel {channel_id}"
        channel_tvg_id = channel.get("tvg_id")
        logo_id = channel.get("logo_id")
        channel_logo_url = f"/api/channels/logos/{logo_id}/cache" if logo_id else channel.get("logo_url")

        store = load_store()
        for existing_sid, existing_session in store.items():
            if existing_session.get("channel_id") == int(channel_id):
                if epg_event_title is not None:
                    existing_session["epg_event_title"] = epg_event_title
                    existing_session["epg_event_description"] = epg_event_description
                    existing_session["epg_event_start"] = epg_event_start
                    existing_session["epg_event_end"] = epg_event_end
                    existing_session["epg_event_id"] = epg_event_id
                    save_store(store)
                    logger.info(
                        f"Updated EPG info on existing AceStream session {existing_sid} "
                        f"for channel {channel_id}: '{epg_event_title}'"
                    )
                else:
                    logger.info(f"Reusing existing AceStream session {existing_sid} for channel {channel_id}")
                return {
                    "session_id": existing_sid,
                    "message": "Reused existing AceStream channel session",
                    "monitor_count": len(existing_session.get("entries", [])),
                }, 200

        resolved_regex = ""
        resolved_match_by_tvg_id = False
        try:
            from apps.automation.automated_stream_manager import RegexChannelMatcher

            regex_matcher = RegexChannelMatcher()
            group_id = channel.get("group_id") if channel.get("group_id") is not None else channel.get("channel_group_id")
            match_config = regex_matcher.get_channel_match_config(str(channel_id), group_id)
            resolved_match_by_tvg_id = match_config.get("match_by_tvg_id", False)
            channel_regex = regex_matcher.get_channel_regex_filter(str(channel_id), default=None, group_id=group_id)
            if channel_regex:
                resolved_regex = channel_regex
            logger.info(
                f"AceStream session for channel {channel_id}: channel profile "
                f"(regex='{resolved_regex}', match_by_tvg_id={resolved_match_by_tvg_id})"
            )
        except Exception as exc:
            logger.debug(f"Could not load channel automation profile for AceStream session: {exc}")

        compiled_regex = None
        if resolved_regex:
            try:
                pattern = resolved_regex.replace("CHANNEL_NAME", re.escape(channel_name))
                pattern = re.sub(r"(?<!\\) +", r"\\s+", pattern)
                compiled_regex = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                logger.error(f"Invalid regex for channel {channel_id} automation profile: {exc}")
                compiled_regex = None

        all_streams = udi.get_streams() or []
        candidate_stream_ids = []
        for stream in all_streams:
            matched = False
            if resolved_match_by_tvg_id and channel_tvg_id:
                if stream.get("tvg_id") == channel_tvg_id:
                    matched = True
            if not matched and compiled_regex:
                stream_name = (stream.get("name") or "")[:1000]
                if compiled_regex.search(stream_name):
                    matched = True
            if matched:
                candidate_stream_ids.append(stream.get("id"))

        if not candidate_stream_ids and not resolved_regex and not (resolved_match_by_tvg_id and channel_tvg_id):
            candidate_stream_ids = channel.get("streams", [])

        entries = []
        seen_content_ids = set()
        for stream_id in candidate_stream_ids:
            stream = udi.get_stream_by_id(int(stream_id)) if stream_id else None
            if not stream:
                continue
            content_id = normalize_content_id(stream.get("url"))
            if not content_id or content_id in seen_content_ids:
                continue
            seen_content_ids.add(content_id)

            payload = {
                "content_id": content_id,
                "stream_name": stream.get("name") or channel_name,
                "interval_s": float(interval_s),
                "run_seconds": int(run_seconds),
                "per_sample_timeout_s": float(per_sample_timeout_s),
                "engine_container_id": engine_container_id if engine_container_id else None,
            }
            started = client.start_session(payload)
            monitor_id = started.get("monitor_id") if isinstance(started, dict) else None
            if not monitor_id:
                continue
            entries.append(
                {
                    "stream_id": stream.get("id"),
                    "stream_name": stream.get("name"),
                    "content_id": content_id,
                    "monitor_id": monitor_id,
                    "ffprobe_stats": extract_stream_stats(stream),
                }
            )

        if not entries:
            return {"error": "No AceStream-compatible streams found matching the discovery criteria"}, 400

        session_id = f"ace_channel_{int(channel_id)}_{int(time.time())}"
        store = load_store()
        store[session_id] = {
            "session_id": session_id,
            "channel_id": int(channel_id),
            "channel_name": channel_name,
            "channel_logo_url": channel_logo_url,
            "channel_tvg_id": channel_tvg_id,
            "regex_filter": resolved_regex,
            "match_by_tvg_id": bool(resolved_match_by_tvg_id),
            "epg_event_title": epg_event_title,
            "epg_event_description": epg_event_description,
            "epg_event_start": epg_event_start,
            "epg_event_end": epg_event_end,
            "epg_event_id": epg_event_id,
            "enable_loop_detection": enable_loop_detection,
            "created_at": time.time(),
            "entries": entries,
        }
        save_store(store)

        logger.info(
            f"Created AceStream session {session_id} for channel {channel_id} "
            f"with {len(entries)} stream(s) "
            f"(filter: '{resolved_regex or 'channel-streams-fallback'}', tvg_id: {resolved_match_by_tvg_id})"
        )
        return {
            "session_id": session_id,
            "message": "AceStream channel session created",
            "monitor_count": len(entries),
        }, 201
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", 502)
        detail = None
        if getattr(exc, "response", None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return {"error": "Failed to create AceStream channel session", "detail": detail}, status_code
    except Exception as exc:
        logger.error(f"Error creating AceStream channel session: {exc}", exc_info=True)
        return {"error": "Internal Server Error"}, 500
