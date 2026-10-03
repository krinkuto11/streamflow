"""Snapshots responsibilities for the shared StreamCheckerService instance."""

import logging
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from apps.stream.stale_status_snapshot import build_dispatcharr_stale_snapshot, build_stale_warnings
from apps.automation.channel_visibility_automation import resolve_channel_visibility_config

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerSnapshotsMixin:
    def _visibility_changelog_result(self, result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not result:
            return None
        if result.get('action') in {'disabled', 'no_visibility_change', 'visible_unmanaged'}:
            return None
        return result


    @staticmethod
    def _automation_profile_progress_context(
        profile: Optional[Dict[str, Any]],
        *,
        forced_profile_id: Optional[str] = None,
    ) -> Dict[str, str]:
        if not isinstance(profile, dict):
            return {}
        profile_id = profile.get('id') or forced_profile_id
        profile_name = profile.get('name')
        context: Dict[str, str] = {}
        if profile_id not in (None, ''):
            context['automation_profile_id'] = str(profile_id)
        if profile_name:
            context['automation_profile_name'] = str(profile_name)
        context['automation_profile_source'] = 'forced' if forced_profile_id else 'resolved'
        context['run_profile_id'] = context.get('automation_profile_id')
        context['run_profile_name'] = context.get('automation_profile_name')
        context['run_profile_source'] = context.get('automation_profile_source')
        context['quality_profile_id'] = context.get('automation_profile_id')
        context['quality_profile_name'] = context.get('automation_profile_name')
        context['quality_profile_source'] = context.get('automation_profile_source')
        context['capacity_profile_name'] = 'Provider account profiles'
        context['capacity_profile_source'] = 'm3u_account_profiles'
        return context


    @staticmethod
    def _single_channel_snapshot_size_bytes(snapshot: Dict[str, Any]) -> int:
        try:
            return len(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return 0


    def _bound_single_channel_run_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        bounded = dict(snapshot or {})
        if self._single_channel_snapshot_size_bytes(bounded) <= self.SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES:
            bounded["snapshot_size_bytes"] = self._single_channel_snapshot_size_bytes(bounded)
            bounded["snapshot_truncated"] = False
            return bounded

        for key in ("effective_profiles", "quality_rules", "feature_flags", "dispatcharr_status", "result_summary", "stale_warnings"):
            value = bounded.get(key)
            if isinstance(value, list):
                bounded[f"{key}_omitted_count"] = max(0, len(value) - 3)
                bounded[key] = value[:3]
            elif isinstance(value, dict):
                bounded[f"{key}_omitted"] = True
                bounded[key] = {}
            bounded["snapshot_truncated"] = True
            if self._single_channel_snapshot_size_bytes(bounded) <= self.SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES:
                bounded["snapshot_size_bytes"] = self._single_channel_snapshot_size_bytes(bounded)
                return bounded

        minimal = {
            "schema_version": bounded.get("schema_version", 1),
            "run_id": bounded.get("run_id"),
            "run_mode": bounded.get("run_mode"),
            "start_source": bounded.get("start_source"),
            "started_at": bounded.get("started_at"),
            "completed_at": bounded.get("completed_at"),
            "streamflow_version": bounded.get("streamflow_version"),
            "streamflow_commit": bounded.get("streamflow_commit"),
            "snapshot_truncated": True,
            "snapshot_omitted_reason": "max_bytes_exceeded",
        }
        minimal["snapshot_size_bytes"] = self._single_channel_snapshot_size_bytes(minimal)
        return minimal


    @staticmethod
    def _single_channel_visibility_summary(visibility_result: Optional[Dict[str, Any]]) -> Dict[str, int]:
        if not isinstance(visibility_result, dict) or not visibility_result.get("changed"):
            return {
                "channels_hidden": 0,
                "channels_ready": 0,
                "channel_visibility_changed": 0,
            }
        action = visibility_result.get("action")
        return {
            "channels_hidden": 1 if action == "hidden" else 0,
            "channels_ready": 1 if action == "unhidden" else 0,
            "channel_visibility_changed": 1,
        }


    def _build_single_channel_run_snapshot(
        self,
        *,
        channel_id: int,
        channel_name: str,
        start_time: float,
        completed_at: datetime,
        duration_seconds: int,
        profile: Optional[Dict[str, Any]],
        profile_progress_context: Dict[str, Any],
        check_stats: Dict[str, Any],
        visibility_summary: Dict[str, int],
        checking_enabled: bool,
        matching_enabled: bool,
        m3u_update_enabled: bool,
        forced_profile_id: Optional[str],
        force_check: bool,
        provider_limit_override: bool,
        is_epg_scheduled: bool,
        m3u_refresh_scope: str,
        m3u_refresh_account_count: int,
        udi: Optional[Any] = None,
    ) -> Dict[str, Any]:
        run_mode = profile_progress_context.get("run_mode") or "single_channel_check"
        if run_mode == "teamarr_preflight":
            start_source = "teamarr_preflight"
        elif is_epg_scheduled:
            start_source = "epg_scheduled"
        elif forced_profile_id:
            start_source = "manual_forced_profile"
        else:
            start_source = "manual"

        version_context = self._streamflow_version_context()
        stream_checking = profile.get("stream_checking", {}) if isinstance(profile, dict) else {}
        dispatcharr_status: Dict[str, Any] = {}
        network_ready: Optional[bool] = None
        m3u_accounts: Optional[List[Dict[str, Any]]] = None
        try:
            if udi and hasattr(udi, "is_network_ready"):
                network_ready = bool(udi.is_network_ready())
                dispatcharr_status["network_ready"] = network_ready
        except Exception as exc:
            dispatcharr_status["network_ready_error"] = type(exc).__name__
        try:
            account_getter = getattr(udi, "get_m3u_accounts", None)
            if callable(account_getter):
                candidate_accounts = account_getter()
                if isinstance(candidate_accounts, list):
                    m3u_accounts = candidate_accounts
        except Exception as exc:
            dispatcharr_status["m3u_accounts_error"] = type(exc).__name__
        dispatcharr_status["stale_status"] = build_dispatcharr_stale_snapshot(
            network_ready=network_ready,
            accounts=m3u_accounts,
        )
        stale_warnings = build_stale_warnings(dispatcharr_stale=dispatcharr_status["stale_status"])

        profile_id = profile_progress_context.get("run_profile_id")
        profile_name = profile_progress_context.get("run_profile_name")
        snapshot = {
            "schema_version": 1,
            "run_id": f"{run_mode}-{channel_id}-{int(start_time)}",
            "run_mode": run_mode,
            "start_source": start_source,
            "started_at": datetime.fromtimestamp(start_time).isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": duration_seconds,
            "streamflow_version": version_context["version"],
            "streamflow_commit": version_context["commit"],
            "channel_id": channel_id,
            "channel_name": channel_name,
            "forced_profile_id": str(forced_profile_id) if forced_profile_id else None,
            "force_check": bool(force_check),
            "provider_limit_override": bool(provider_limit_override),
            "is_epg_scheduled": bool(is_epg_scheduled),
            "effective_profiles": [{
                "profile_id": profile_id,
                "profile_name": profile_name,
                "profile_source": profile_progress_context.get("run_profile_source"),
                "channel_count": 1,
                "quality_rules_enabled": bool(checking_enabled),
                "check_all_streams": bool(stream_checking.get("check_all_streams", False)),
                "stream_limit": stream_checking.get("stream_limit", 0),
            }],
            "effective_profile_count": 1 if profile_name or profile_id else 0,
            "channel_count": 1,
            "quality_rules": [{
                "profile_id": profile_progress_context.get("quality_profile_id"),
                "profile_name": profile_progress_context.get("quality_profile_name"),
                "enabled": bool(checking_enabled),
                "check_all_streams": bool(stream_checking.get("check_all_streams", False)),
                "stream_limit": stream_checking.get("stream_limit", 0),
            }],
            "capacity_profile_context": {
                "type": "provider_account_profiles",
                "description": "Capacity is enforced by account limits and active provider profiles.",
                "profile_limited": not bool(provider_limit_override),
            },
            "feature_flags": {
                "single_channel_checking": True,
                "m3u_update_enabled": bool(m3u_update_enabled),
                "stream_matching_enabled": bool(matching_enabled),
                "stream_checking_enabled": bool(checking_enabled),
                "force_check": bool(force_check),
                "provider_limit_override": bool(provider_limit_override),
            },
            "dispatcharr_status": dispatcharr_status,
            "stale_warnings": stale_warnings,
            "teamarr_status": {
                "preflight_context": run_mode == "teamarr_preflight",
            },
            "m3u_refresh": {
                "scope": m3u_refresh_scope,
                "account_count": int(m3u_refresh_account_count or 0),
            },
            "result_summary": {
                "total_streams": check_stats.get("total_streams", 0),
                "dead_streams": check_stats.get("dead_streams", 0),
                "avg_resolution": check_stats.get("avg_resolution"),
                "avg_bitrate": check_stats.get("avg_bitrate"),
                "avg_fps": check_stats.get("avg_fps"),
                "channels_hidden": visibility_summary.get("channels_hidden", 0),
                "channels_ready": visibility_summary.get("channels_ready", 0),
                "channel_visibility_changed": visibility_summary.get("channel_visibility_changed", 0),
            },
            "limits": {
                "max_bytes": self.SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES,
            },
        }
        return self._bound_single_channel_run_snapshot(snapshot)


    def _apply_channel_visibility_after_check(
        self,
        channel_data: Dict[str, Any],
        *,
        good_streams_count: int,
        dead_streams_count: int,
        failed_streams_count: Optional[int] = None,
        revived_streams_count: int,
        total_streams: int,
        profile: Optional[Dict[str, Any]] = None,
        visibility_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            config = visibility_config
            if config is None:
                config = resolve_channel_visibility_config(
                    self.config.get('channel_visibility_automation', {}),
                    profile,
                )
            result = self.channel_visibility_automation.handle_quality_result(
                channel_data,
                good_streams_count=good_streams_count,
                dead_streams_count=dead_streams_count,
                failed_streams_count=failed_streams_count,
                revived_streams_count=revived_streams_count,
                config=config,
                details={
                    'total_streams': total_streams,
                    'good_streams_count': good_streams_count,
                    'dead_streams_count': dead_streams_count,
                    'failed_streams_count': failed_streams_count,
                    'revived_streams_count': revived_streams_count,
                },
            )
            if self._visibility_changelog_result(result):
                logger.info(
                    "Channel visibility automation action=%s reason=%s channel_id=%s",
                    result.get('action'),
                    result.get('reason'),
                    result.get('channel_id'),
                )
            return result
        except Exception as exc:
            logger.warning("Channel visibility automation failed after check: %s", exc)
            return {
                'action': 'visibility_error',
                'changed': False,
                'reason': 'quality_result',
                'details': {'error': 'channel_visibility_failed'},
            }

