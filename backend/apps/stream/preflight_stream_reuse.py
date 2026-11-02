"""Event-scoped evidence for skipping successful automatic preflight probes."""

import hashlib
import json
import logging
import math
import threading
from datetime import datetime, timezone

from apps.core.stream_stats_utils import extract_stream_stats
from apps.stream.preflight_policy import parse_time

logger = logging.getLogger(__name__)
STATE_KEY = "teamarr_preflight_stream_success_state"
STATE_VERSION = 1
MAX_ENTRIES = 5000
_STATE_LOCK = threading.RLock()
_FLAGS = (
    "measurement_incomplete", "bitrate_recheck_required", "visual_probe_incomplete",
    "blank_detected", "freeze_detected", "loop_detected",
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def stats_blob(source):
    value = source.get("stream_stats", source)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def measurement_fingerprint(source):
    metrics = extract_stream_stats(source)
    # Normalize numeric representations from analyzer vs Dispatcharr JSON.
    for field in ("fps", "bitrate_kbps"):
        value = metrics.get(field)
        metrics[field] = float(value) if value is not None else None
    if metrics["bitrate_kbps"] is not None:
        # StreamStatsWriter persists ffmpeg_output_bitrate as integer kbps.
        metrics["bitrate_kbps"] = int(metrics["bitrate_kbps"])
    facts = stats_blob(source)
    return fingerprint({
        "metrics": {key: metrics.get(key) for key in (
            "resolution", "fps", "bitrate_kbps", "video_codec", "audio_codec", "hdr_format")},
        "flags": {key: bool(facts.get(key)) for key in _FLAGS},
        "reason": facts.get("quality_reason") or "none",
    })


def automatic_reuse_context(event, config):
    if not config.get("skip_known_good_streams") or event.get("trigger_bucket") == "manual":
        return None
    at = parse_time(event.get("event_date"))
    if at is None or not event.get("identity") or not event.get("dispatcharr_channel_id"):
        return None
    return {
        "instance": fingerprint(str(config.get("teamarr_base_url") or "")),
        "identity": event["identity"],
        "kind": event.get("preflight_kind") or "event",
        "event_date": at.isoformat(),
        "channel_id": event["dispatcharr_channel_id"],
        "channel_uuid": event.get("dispatcharr_uuid"),
        "window": {key: (event.get("next_live_window") or {}).get(key)
                   for key in ("start", "stop")},
        "not_before": at.timestamp() - int(config["preflight_offset_minutes"]) * 60,
        "expires_at": at.timestamp() + int(config["post_start_grace_minutes"]) * 60,
        "max_age_seconds": max(60, (int(config["preflight_offset_minutes"]) +
                                   int(config["post_start_grace_minutes"])) * 60),
    }


def source_fingerprint(stream, accounts):
    account_id = stream.get("m3u_account", stream.get("m3u_account_id"))
    if isinstance(account_id, dict):
        account_id = account_id.get("id")
    account = next((item for item in accounts if str(item.get("id")) == str(account_id)), None)
    if account_id not in (None, "", 0, "0") and account is None:
        return None
    account = account or {}
    profiles = account.get("profiles", [])
    if not isinstance(profiles, list) or any(not isinstance(item, dict) for item in profiles):
        return None
    static_profiles = [{key: profile.get(key) for key in (
        "id", "is_active", "is_default", "search_pattern", "replace_pattern",
        "custom_properties", "max_streams")} for profile in profiles]
    static_profiles.sort(key=lambda item: str(item.get("id")))
    # Hash credentials/URL transformations; never persist the URLs themselves.
    return fingerprint({
        "stream": {key: stream.get(key) for key in (
            "id", "name", "url", "tvg_id", "stream_profile_id", "custom_properties")},
        "account_id": account_id,
        "account": {key: account.get(key) for key in (
            "id", "is_active", "server_url", "user_agent", "custom_properties")},
        "profiles": static_profiles,
    })


def complete_measurement(source, profile, is_good):
    facts = stats_blob(source)
    if any(facts.get(key) for key in _FLAGS):
        return False
    if source.get("error") or source.get("provider_limit_skipped") or source.get("cached"):
        return False
    if str(source.get("status", "")).lower() in {
        "cached", "error", "failed", "offline", "timeout", "dead", "incomplete", "skipped", "pending", "deferred",
    }:
        return False
    if facts.get("quality_reason") not in (None, "", "none"):
        return False
    metrics = extract_stream_stats(source)
    try:
        if not math.isfinite(float(metrics.get("bitrate_kbps"))) or float(metrics["bitrate_kbps"]) <= 0:
            return False
        if not math.isfinite(float(metrics.get("fps"))) or float(metrics["fps"]) <= 0:
            return False
        width, height = str(metrics.get("resolution")).lower().split("x")
        if int(width) <= 0 or int(height) <= 0:
            return False
    except (ValueError, TypeError):
        return False
    checking = profile.get("stream_checking") or {}
    for kind in ("blank", "freeze"):
        if checking.get(kind + "_check_enabled") and (
            not facts.get(kind + "_probe_ran") or facts.get("visual_probe_completed") is not True
        ):
            return False
    if facts.get("loop_probe_ran") and facts.get("loop_detected") is not False:
        return False
    return bool(is_good(source))


class PreflightStreamReuse:
    """Read fresh SQL state under one lock; sibling runs cannot overwrite it."""

    def __init__(self, db_provider, clock):
        self.db_provider, self.clock = db_provider, clock

    def _load(self):
        state = self.db_provider().get_system_setting(STATE_KEY, {})
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return {}
        entries = state.get("entries")
        if not isinstance(entries, dict):
            return {}
        now = self.clock()
        valid = {}
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            try:
                measured = float(entry["measured_at"])
                expires = float(entry["expires_at"])
                if math.isfinite(measured) and math.isfinite(expires) and 0 <= measured <= now < expires:
                    valid[key] = entry
            except (KeyError, TypeError, ValueError):
                continue
        return dict(sorted(valid.items(), key=lambda item: item[1]["measured_at"], reverse=True)[:MAX_ENTRIES])

    def prepare(self, context, channel, streams, profile, analysis_config, accounts, is_good, is_dead):
        try:
            now = self.clock()
            if not context or not profile or not isinstance(accounts, list):
                return None
            if any(not isinstance(item, dict) for item in accounts):
                return None
            if str(context["channel_id"]) != str(channel["id"]):
                return None
            if context.get("channel_uuid") and str(context["channel_uuid"]) != str(channel.get("uuid")):
                return None
            if not context["not_before"] <= now < context["expires_at"]:
                return None
            event_key = fingerprint({"context": context, "uuid": channel.get("uuid")})
            policy_key = fingerprint({"profile": profile, "analysis": analysis_config})
            sources = {str(item["id"]): source_fingerprint(item, accounts) for item in streams}
            with _STATE_LOCK:
                entries = self._load()
            reused = {}
            for stream in streams:
                sid = str(stream["id"])
                entry = entries.get(event_key + ":" + sid, {})
                if not entry or not sources[sid] or is_dead(stream):
                    continue
                if (entry.get("source") == sources[sid] and entry.get("policy") == policy_key
                        and 0 <= now - float(entry["measured_at"]) < context["max_age_seconds"]
                        and complete_measurement(stream, profile, is_good)
                        and entry.get("measurement") == measurement_fingerprint(stream)):
                    reused[stream["id"]] = entry["measured_at"]
            return ReuseCheck(self, context, event_key, policy_key, sources, streams,
                              profile, is_good, reused, now)
        except Exception:
            logger.warning("Preflight success evidence unavailable; checking streams normally")
            return None


class ReuseCheck:
    def __init__(self, store, context, event_key, policy_key, sources, streams, profile, is_good, reused, started_at):
        self.store, self.context, self.event_key, self.policy_key = store, context, event_key, policy_key
        self.sources, self.profile, self.is_good = sources, profile, is_good
        self.reused, self.started_at = reused, started_at
        self.target_ids = [item["id"] for item in streams if item["id"] not in reused]

    def checked_at(self, stream_id):
        return datetime.fromtimestamp(self.reused[stream_id], timezone.utc).isoformat()

    def finish(self, analyzed, checked_ids, dead_ids):
        try:
            with _STATE_LOCK:
                entries = self.store._load()
                for item in analyzed:
                    sid = item.get("stream_id")
                    if sid not in checked_ids or item.get("status") == "cached" or item.get("cached"):
                        continue
                    key = self.event_key + ":" + str(sid)
                    entries.pop(key, None)
                    source = self.sources.get(str(sid))
                    if source and sid not in dead_ids and complete_measurement(item, self.profile, self.is_good):
                        entries[key] = {
                            "source": source, "policy": self.policy_key,
                            "measurement": measurement_fingerprint(item),
                            "measured_at": self.started_at,
                            "expires_at": min(self.context["expires_at"],
                                              self.started_at + self.context["max_age_seconds"]),
                        }
                entries = dict(sorted(entries.items(), key=lambda pair: pair[1]["measured_at"], reverse=True)[:MAX_ENTRIES])
                self.store.db_provider().set_system_setting(STATE_KEY, {
                    "version": STATE_VERSION, "entries": entries,
                })
        except Exception:
            logger.warning("Could not save preflight success evidence; future probes remain enabled")
