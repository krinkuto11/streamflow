#!/usr/bin/env python3
"""
Automated Stream Manager for Dispatcharr

This module handles the automated process of:
1. Updating M3U playlists
2. Discovering new streams and assigning them to channels via regex
3. Maintaining changelog of updates

Uses the Universal Data Index (UDI) as the single source of truth for data access.
"""

import json
import logging
import os
import re
import time
import threading
import copy
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any, Union
import concurrent.futures
from collections import defaultdict

# Pre-compiled regex pattern for whitespace conversion (performance optimization)
# This pattern matches one or more spaces that are NOT preceded by a backslash
# Used to convert literal spaces to flexible whitespace while preserving escaped spaces
_WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')

# Placeholder for CHANNEL_NAME variable during regex validation
# Used to substitute CHANNEL_NAME in patterns before compiling for validation
_CHANNEL_NAME_PLACEHOLDER = 'PLACEHOLDER'


class RefreshResult(tuple):
    """Tuple-compatible playlist refresh result with V6 outcome metadata."""

    def __new__(
        cls,
        success: bool,
        accounts: Optional[List[Dict[str, Any]]] = None,
        *,
        failed_refresh_requests: Optional[List[Dict[str, Any]]] = None,
        outcome: Optional[str] = None,
    ):
        obj = super().__new__(cls, (success, accounts or []))
        obj.failed_refresh_requests = list(failed_refresh_requests or [])
        obj.failed_refresh_request_count = len(obj.failed_refresh_requests)
        obj.degraded = bool(success and obj.failed_refresh_request_count > 0)
        obj.outcome = outcome or (
            "completed_degraded"
            if obj.degraded
            else "completed"
            if success
            else "failed"
        )
        return obj

    def __bool__(self) -> bool:
        return bool(self[0])


@lru_cache(maxsize=50000)
def _compile_stream_search_regex(pattern: str, channel_name: str, case_sensitive: bool) -> re.Pattern:
    """Compile and cache stream-matching regex patterns for reuse."""
    substituted_pattern = pattern.replace('CHANNEL_NAME', re.escape(channel_name))
    search_pattern = substituted_pattern
    search_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern)
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(search_pattern, flags)

# Import croniter for cron expression support
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False

from apps.core.api_utils import (
    refresh_m3u_playlists,
    get_m3u_accounts,
    get_streams,
    add_streams_to_channel,
    update_channel_streams,
    _get_base_url
)

# Import UDI for direct data access
from apps.udi import get_udi_manager
from apps.udi.fetcher import FetchCancelled
from apps.stream.stale_status_snapshot import build_dispatcharr_stale_snapshot, build_stale_warnings
from apps.automation.automation_config_manager import get_automation_config_manager
from apps.automation.channel_visibility_automation import (
    ChannelVisibilityAutomation,
    resolve_channel_visibility_config,
)
from apps.automation.regex_settings import (
    default_channel_regex_global_settings,
    normalize_channel_regex_global_settings,
)

# Import channel settings manager
# Import channel settings manager - DEPRECATED/REMOVED
# from channel_settings_manager import get_channel_settings_manager

# Import profile config
# Import profile config - DEPRECATED/REMOVED
# from profile_config import get_profile_config

# Setup centralized logging
from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_state_change

logger = setup_logging(__name__)

# Import DeadStreamsTracker
try:
    from apps.stream.dead_streams_tracker import DeadStreamsTracker
    DEAD_STREAMS_TRACKER_AVAILABLE = True
except ImportError:
    DEAD_STREAMS_TRACKER_AVAILABLE = False
    logger.warning("DeadStreamsTracker not available. Dead stream filtering will be disabled.")

# Configuration directory - persisted via Docker volume
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


def get_channels(*args, **kwargs):
    """Legacy patch target for tests/scripts that predate UDI direct access."""
    return get_udi_manager().get_channels(*args, **kwargs)


def assign_streams_to_channel(channel_id: int, stream_ids: List[int], allow_dead_streams: bool = False):
    """Legacy wrapper kept so old patch targets affect stream assignment."""
    return add_streams_to_channel(channel_id, stream_ids, allow_dead_streams=allow_dead_streams)


def get_stream_checker_service():
    """Legacy patch target for tests that mocked the old import location."""
    import importlib
    legacy_module = importlib.import_module("stream_checker_service")
    return legacy_module.get_stream_checker_service()


try:
    from channel_settings_manager import get_channel_settings_manager
except Exception:  # pragma: no cover - compatibility fallback
    def get_channel_settings_manager():
        return None


class ChangelogManager:
    """Manages changelog entries for stream updates."""
    
    def __init__(self, changelog_file=None):
        if changelog_file is None:
            changelog_file = CONFIG_DIR / "changelog.json"
        self.changelog_file = Path(changelog_file)
        self.changelog = [] # deprecated but kept for backwards comp
    
    def _load_changelog(self) -> List[Dict]:
        """Deprecated."""
        return []
    
    def add_entry(self, action: str, details: Dict, timestamp: Optional[str] = None, subentries: Optional[List[Dict[str, Any]]] = None):
        """Add a new changelog entry."""
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        entry = {
            "action": action,
            "details": copy.deepcopy(details),
            "timestamp": timestamp,
        }
        if subentries is not None:
            entry["subentries"] = copy.deepcopy(subentries)
        if self._has_channel_updates(entry):
            self.changelog.append(entry)

        # New telemetry DB logic
        try:
            from apps.telemetry.telemetry_db import save_automation_run_telemetry, save_generic_telemetry
            if action == 'automation_run':
                save_automation_run_telemetry(action, details, subentries, timestamp)
            else:
                save_generic_telemetry(action, details, subentries, timestamp)
            logger.info(f"Telemetry entry added: {action}")
        except Exception as e:
            logger.error(f"Failed to process telemetry: {e}")
    
    def _save_changelog(self):
        """Deprecated."""
        pass
    
    def get_recent_entries(self, days: int = 7) -> List[Dict]:
        """Deprecated: The UI will update to use the new Telemetry API."""
        cutoff = datetime.now() - timedelta(days=days)
        recent_entries = []
        for entry in self.changelog:
            try:
                entry_time = datetime.fromisoformat(entry.get("timestamp", ""))
            except (TypeError, ValueError):
                entry_time = datetime.now()
            if entry_time >= cutoff:
                recent_entries.append(copy.deepcopy(entry))
        return list(reversed(recent_entries))
    
    def add_playlist_update_entry(self, channels_updated: Dict[int, Dict], global_stats: Dict):
        """Add a playlist update & match entry with subentries.
        
        Args:
            channels_updated: Dict mapping channel_id to update info (streams added, stats, logo_url)
            global_stats: Global statistics (total streams added, dead streams, avg resolution, avg bitrate)
        """
        # Create subentries for streams matched per channel
        update_subentries = []
        for channel_id, info in channels_updated.items():
            if info.get('streams_added'):
                update_subentries.append({
                    'type': 'update_match',
                    'channel_id': channel_id,
                    'channel_name': info.get('channel_name', f'Channel {channel_id}'),
                    'logo_url': info.get('logo_url'),
                    'streams': info.get('streams_added', [])
                })
        
        # Create subentries for channel checks
        check_subentries = []
        for channel_id, info in channels_updated.items():
            if info.get('check_stats'):
                check_subentries.append({
                    'type': 'check',
                    'channel_id': channel_id,
                    'channel_name': info.get('channel_name', f'Channel {channel_id}'),
                    'logo_url': info.get('logo_url'),
                    'stats': info.get('check_stats', {})
                })
        
        subentries = []
        if update_subentries:
            subentries.append({'group': 'update_match', 'items': update_subentries})
        if check_subentries:
            subentries.append({'group': 'check', 'items': check_subentries})
        
        self.add_entry(
            action='playlist_update_match',
            details=global_stats,
            subentries=subentries
        )
    
    def add_global_check_entry(self, channels_checked: Dict[int, Dict], global_stats: Dict):
        """Add a global check entry with subentries.
        
        Args:
            channels_checked: Dict mapping channel_id to check stats (including logo_url)
            global_stats: Global statistics across all channels
        """
        check_subentries = []
        for channel_id, stats in channels_checked.items():
            check_subentries.append({
                'type': 'check',
                'channel_id': channel_id,
                'channel_name': stats.get('channel_name', f'Channel {channel_id}'),
                'logo_url': stats.get('logo_url'),
                'stats': stats
            })
        
        subentries = [{'group': 'check', 'items': check_subentries}] if check_subentries else []
        
        self.add_entry(
            action='global_check',
            details=global_stats,
            subentries=subentries
        )
    
    def add_single_channel_check_entry(self, channel_id: int, channel_name: str, check_stats: Dict, logo_url: Optional[str] = None, program_name: Optional[str] = None):
        """Add a single channel check entry.
        
        Args:
            channel_id: ID of the channel checked
            channel_name: Name of the channel
            check_stats: Statistics from the channel check
            logo_url: Optional URL for the channel logo
            program_name: Optional program name if this was a scheduled EPG check
        """
        check_subentries = [{
            'type': 'check',
            'channel_id': channel_id,
            'channel_name': channel_name,
            'logo_url': logo_url,
            'stats': check_stats
        }]
        
        subentries = [{'group': 'check', 'items': check_subentries}]
        
        # Build details dict
        details = {
            'channel_id': channel_id,
            'channel_name': channel_name,
            'total_streams': check_stats.get('total_streams', 0),
            'dead_streams': check_stats.get('dead_streams', 0),
            'avg_resolution': check_stats.get('avg_resolution', 'N/A'),
            'avg_bitrate': check_stats.get('avg_bitrate', 'N/A')
        }
        v7_detail_fields = (
            'avg_fps',
            'duration',
            'duration_seconds',
            'run_mode',
            'run_profile_id',
            'run_profile_name',
            'run_profile_source',
            'quality_profile_id',
            'quality_profile_name',
            'quality_profile_source',
            'capacity_profile_name',
            'capacity_profile_source',
            'channels_hidden',
            'channels_ready',
            'channel_visibility_changed',
            'run_snapshot',
        )
        for field in v7_detail_fields:
            if field in check_stats:
                details[field] = check_stats.get(field)
        
        # Add program name if provided (for scheduled EPG checks)
        if program_name:
            details['program_name'] = program_name
        
        self.add_entry(
            action='single_channel_check',
            details=details,
            subentries=subentries
        )
    
    def add_automation_run_entry(self, run_results: Dict[str, Any]):
        """Add a consolidated automation run entry.
        
        Args:
            run_results: Dictionary containing periods, channels, and their step results.
        """
        self.add_entry(
            action='automation_run',
            details=run_results
        )

    def _has_channel_updates(self, entry: Dict) -> bool:
        """Check if a changelog entry contains meaningful channel/stream updates."""
        details = entry.get('details', {})
        action = entry.get('action', '')
        
        # For automation_run, always include
        if action == 'automation_run':
            return True
            
        # For new action types, check if they have subentries
        if action in ['playlist_update_match', 'global_check', 'single_channel_check']:
            subentries = entry.get('subentries', [])
            # Include if there are subentries with items
            return any(group.get('items') for group in subentries)
        
        # For playlist_refresh, only include if there were actual changes
        if action == 'playlist_refresh':
            added = details.get('added_streams', [])
            removed = details.get('removed_streams', [])
            return len(added) > 0 or len(removed) > 0
        
        # For streams_assigned, only include if streams were actually assigned
        if action == 'streams_assigned':
            total_assigned = details.get('total_assigned', 0)
            return total_assigned > 0
        
        # For other actions, include if success is True or not specified
        # (exclude failed operations without updates)
        if 'success' in details:
            return details['success'] is True
        
        return True  # Include entries without explicit success flag


class RegexChannelMatcher:
    """Handles regex-based channel matching for stream assignment.

    Patterns are stored in the ``channel_regex_configs`` and
    ``channel_regex_patterns`` SQL tables via the DAL.  The legacy
    in-memory ``channel_patterns`` dict is kept populated from the DB so
    that no callers need to change (hot-path matching code reads the dict).
    """
    
    def __init__(self, config_file=None):
        # config_file kept for backward compatibility (tests, one-time migration).
        # If a path is given and the file exists, the patterns are seeded into the
        # SQL database before loading.  In production the parameter is unused.
        self.lock = threading.RLock()
        self._config_file: Optional[Path] = None
        self._manual_regex_config = False
        self._explicit_regex_config_file = config_file is not None
        self._file_backed_compat = self._explicit_regex_config_file or str(CONFIG_DIR) != "/app/data"
        self._config_file_signature: Optional[Tuple[int, int]] = None
        if config_file is None:
            config_file = CONFIG_DIR / "channel_regex_config.json"
        config_file = Path(config_file)
        self._config_file = config_file
        if self._file_backed_compat and config_file.exists():
            self._seed_from_config_file(config_file)
            self._remember_config_file_signature()
        self.group_patterns_key = 'group_regex_patterns'
        self.channel_patterns = self._load_patterns()
        self.group_patterns = self._load_group_patterns()

    @property
    def regex_config(self) -> Dict[str, Any]:
        """Legacy alias for older tests/scripts that used ``regex_config``."""
        return self.channel_patterns

    @regex_config.setter
    def regex_config(self, value: Dict[str, Any]) -> None:
        self.channel_patterns = self._normalize_legacy_regex_config(value or {})
        self._manual_regex_config = True
        self._clear_runtime_caches()

    def _normalize_legacy_regex_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Convert old ``channels`` regex config payloads into V2 pattern shape."""
        if not isinstance(config, dict):
            return {"patterns": {}, "global_settings": {}}

        if "patterns" in config:
            normalized = copy.deepcopy(config)
            normalized.setdefault("global_settings", {})
            for pattern_data in normalized.get("patterns", {}).values():
                if isinstance(pattern_data, dict) and "regex" not in pattern_data:
                    pattern_data["regex"] = [
                        p.get("pattern")
                        for p in pattern_data.get("regex_patterns", [])
                        if isinstance(p, dict) and p.get("pattern")
                    ]
            return normalized

        patterns: Dict[str, Any] = {}
        for channel in config.get("channels", []) or []:
            if not isinstance(channel, dict):
                continue
            channel_id = channel.get("channel_id", channel.get("id"))
            if channel_id is None:
                continue

            regex_patterns = []
            raw_patterns = channel.get("patterns", channel.get("regex_patterns", [])) or []
            for priority, item in enumerate(raw_patterns):
                if isinstance(item, dict):
                    pattern = item.get("pattern") or item.get("regex")
                    if not pattern:
                        continue
                    regex_patterns.append({
                        "pattern": pattern,
                        "m3u_accounts": item.get("m3u_accounts"),
                        "priority": item.get("priority", priority),
                    })
                else:
                    regex_patterns.append({
                        "pattern": str(item),
                        "m3u_accounts": None,
                        "priority": priority,
                    })

            patterns[str(channel_id)] = {
                "name": channel.get("channel_name", channel.get("name", "")),
                "enabled": channel.get("enabled", True),
                "match_by_tvg_id": channel.get("match_by_tvg_id", False),
                "regex": [p["pattern"] for p in regex_patterns],
                "regex_patterns": regex_patterns,
            }

        return {
            "patterns": patterns,
            "global_settings": config.get("global_settings", {}),
        }

    def _seed_from_config_file(self, config_file: Path):
        """Read a JSON config file and import the patterns into SQL.

        Used for backward-compat test setup and one-time migration paths.
        Any import errors are logged and silently swallowed so that the
        matcher still initialises with whatever state is already in the DB.
        """
        import json as _json
        try:
            with open(config_file, 'r') as fh:
                data = _json.load(fh)
            from apps.database.manager import get_db_manager
            db = get_db_manager()
            db.import_channel_regex_configs_from_json(data, merge=False)
            if isinstance(data.get('global_settings'), dict):
                db.set_system_setting('channel_regex_global_settings', data['global_settings'])
        except Exception as exc:
            logger.warning(
                f"Could not seed regex config from {config_file}: "
                f"{type(exc).__name__}: {exc}"
            )

    def _remember_config_file_signature(self) -> None:
        """Record the legacy config file state after we import or write it."""
        if self._config_file is None:
            self._config_file_signature = None
            return
        try:
            stat = self._config_file.stat()
            self._config_file_signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._config_file_signature = None

    def _config_file_changed_since_last_import(self) -> bool:
        if not self._file_backed_compat or self._config_file is None or not self._config_file.exists():
            return False
        try:
            stat = self._config_file.stat()
        except OSError:
            return False
        return self._config_file_signature != (stat.st_mtime_ns, stat.st_size)
    
    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_in_memory(self, configs: Dict[str, Any], global_settings: Dict[str, Any]) -> Dict:
        """Build the canonical in-memory dict from DAL data."""
        configs = copy.deepcopy(configs or {})
        for pattern_data in configs.values():
            if isinstance(pattern_data, dict) and "regex" not in pattern_data:
                pattern_data["regex"] = [
                    p.get("pattern")
                    for p in pattern_data.get("regex_patterns", [])
                    if isinstance(p, dict) and p.get("pattern")
                ]
        return {
            'patterns': configs,
            'global_settings': global_settings,
        }

    def _load_patterns(self) -> Dict:
        """Load regex patterns from SQL and build the in-memory cache.

        **Side-effects on the database**: If any stored patterns fail regex
        compilation they are permanently removed from the database during this
        call.  Channels whose *entire* pattern list is invalid are deleted.
        Callers that need explicit control over clean-up should call a
        dedicated validate method instead.

        Falls back to the legacy ``channel_regex_config`` SystemSetting JSON
        blob if the new tables are empty, migrating the data transparently.
        """
        if self._config_file is not None and self._file_backed_compat and not self._config_file.exists():
            empty = {
                "patterns": {},
                "global_settings": default_channel_regex_global_settings(),
            }
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_file, 'w', encoding='utf-8') as fh:
                json.dump(empty, fh, indent=2)
            return empty

        from apps.database.manager import get_db_manager
        db = get_db_manager()

        configs = db.get_all_channel_regex_configs()
        global_settings = db.get_system_setting(
            'channel_regex_global_settings',
            default_channel_regex_global_settings(),
        )
        global_settings = normalize_channel_regex_global_settings(global_settings)

        # --- One-time migration from legacy SystemSetting JSON blob ---
        if not configs:
            legacy = db.get_system_setting('channel_regex_config')
            if legacy and isinstance(legacy, dict) and 'patterns' in legacy:
                logger.info("Migrating regex patterns from SystemSetting JSON blob to dedicated SQL tables")
                imported, errors = db.import_channel_regex_configs_from_json(legacy)
                if errors:
                    logger.warning(f"Migration errors: {errors}")
                # Preserve global_settings from legacy blob
                if 'global_settings' in legacy:
                    global_settings = normalize_channel_regex_global_settings(legacy['global_settings'])
                    db.set_system_setting('channel_regex_global_settings', global_settings)
                # Clear the old blob to avoid re-migration on next start
                db.set_system_setting('channel_regex_config', None)
                configs = db.get_all_channel_regex_configs()

        # Validate and clean up invalid regex patterns
        configs_to_remove = []
        for channel_id, cfg in list(configs.items()):
            valid_patterns = []
            has_invalid = False
            for pat_obj in cfg.get('regex_patterns', []):
                pattern = pat_obj.get('pattern', '')
                if not pattern:
                    has_invalid = True
                    continue
                try:
                    validation_pattern = pattern.replace('CHANNEL_NAME', _CHANNEL_NAME_PLACEHOLDER)
                    re.compile(validation_pattern)
                    valid_patterns.append(pat_obj)
                except re.error as e:
                    logger.warning(f"Removing invalid regex '{pattern}' for channel {channel_id}: {e}")
                    has_invalid = True

            if has_invalid:
                if valid_patterns:
                    cfg['regex_patterns'] = valid_patterns
                    db.upsert_channel_regex_config(
                        channel_id=str(channel_id),
                        name=cfg.get('name', ''),
                        enabled=cfg.get('enabled', True),
                        match_by_tvg_id=cfg.get('match_by_tvg_id', False),
                        regex_patterns=valid_patterns,
                    )
                else:
                    configs_to_remove.append(channel_id)

        for cid in configs_to_remove:
            del configs[cid]
            db.delete_channel_regex_config(str(cid))

        return self._build_in_memory(configs, global_settings)

    def _save_patterns(self, patterns: Dict):
        """Persist patterns dict to SQL (used by import and legacy callers)."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        patterns_dict = patterns.get('patterns', {})
        global_settings = normalize_channel_regex_global_settings(patterns.get('global_settings', {}))
        imported, errors = db.import_channel_regex_configs_from_json(
            {'patterns': patterns_dict}, merge=False
        )
        if errors:
            logger.error(f"Error saving patterns to SQL: {errors}")
        if global_settings:
            db.set_system_setting('channel_regex_global_settings', global_settings)
        legacy_payload = {'patterns': patterns_dict}
        if global_settings:
            legacy_payload['global_settings'] = global_settings
        db.set_system_setting('channel_regex_config', legacy_payload)
        if self._config_file is not None and self._file_backed_compat:
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_file, 'w', encoding='utf-8') as fh:
                json.dump(legacy_payload, fh, indent=2)
            self._remember_config_file_signature()
        # Keep in-memory cache in sync
        self.channel_patterns = self._build_in_memory(
            db.get_all_channel_regex_configs(), global_settings
        )
        self._clear_runtime_caches()

    def _clear_runtime_caches(self):
        """Clear hot-path caches used during stream matching."""
        _compile_stream_search_regex.cache_clear()

    def _load_group_patterns(self) -> Dict[str, Any]:
        """Load group-level regex pattern config from system settings."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        data = db.get_system_setting(self.group_patterns_key, {}) or {}
        if not isinstance(data, dict):
            return {}
        return data

    def _get_group_patterns(self) -> Dict[str, Any]:
        """Get group-level regex pattern config from in-memory cache."""
        with self.lock:
            return dict(self.group_patterns)

    def _save_group_patterns(self, data: Dict[str, Any]) -> bool:
        """Persist group-level regex pattern config to system settings."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        saved = db.set_system_setting(self.group_patterns_key, data)
        if saved:
            with self.lock:
                self.group_patterns = dict(data)
            self._clear_runtime_caches()
        return saved

    def _normalize_regex_patterns(self, regex_patterns: 'Union[List[str], List[Dict]]', m3u_accounts: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Normalize regex patterns to the canonical object format."""
        normalized_patterns: List[Dict[str, Any]] = []

        if isinstance(regex_patterns, list) and len(regex_patterns) > 0:
            if isinstance(regex_patterns[0], dict):
                for item in regex_patterns:
                    if not isinstance(item, dict) or "pattern" not in item:
                        raise ValueError("Each pattern object must have a 'pattern' field")
                    normalized_patterns.append({
                        "pattern": item["pattern"],
                        "m3u_accounts": item.get("m3u_accounts")
                    })
            else:
                for pattern in regex_patterns:
                    normalized_patterns.append({
                        "pattern": pattern,
                        "m3u_accounts": m3u_accounts
                    })
        return normalized_patterns

    def _get_effective_channel_config(self, channel_id: Union[str, int], group_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Return effective channel matching config with channel-over-group precedence."""
        channel_id_str = str(channel_id)
        channel_config = self.channel_patterns.get("patterns", {}).get(channel_id_str)
        if isinstance(channel_config, dict):
            return channel_config

        if group_id is None:
            return {}

        group_config = self.get_group_pattern(str(group_id))
        if not isinstance(group_config, dict):
            return {}

        # Use group-level settings as channel fallback; name is informational only.
        return {
            "name": group_config.get("name", ""),
            "enabled": group_config.get("enabled", True),
            "match_by_tvg_id": group_config.get("match_by_tvg_id", False),
            "regex_patterns": group_config.get("regex_patterns", [])
        }

    def get_group_pattern(self, group_id: Union[str, int]) -> Optional[Dict[str, Any]]:
        """Get regex config for a group."""
        patterns = self._get_group_patterns()
        cfg = patterns.get(str(group_id))
        return cfg if isinstance(cfg, dict) else None

    def add_group_pattern(self, group_id: Union[str, int], name: str, regex_patterns: 'Union[List[str], List[Dict]]', enabled: bool = True, match_by_tvg_id: bool = False, m3u_accounts: Optional[List[int]] = None):
        """Add or update regex pattern config for a group."""
        normalized_patterns = self._normalize_regex_patterns(regex_patterns, m3u_accounts)
        pattern_strings = [p["pattern"] for p in normalized_patterns]
        if pattern_strings:
            is_valid, error_msg = self.validate_regex_patterns(pattern_strings)
            if not is_valid:
                raise ValueError(error_msg)

        patterns = self._get_group_patterns()
        patterns[str(group_id)] = {
            "name": name,
            "enabled": enabled,
            "match_by_tvg_id": bool(match_by_tvg_id),
            "regex_patterns": normalized_patterns
        }
        self._save_group_patterns(patterns)

    def delete_group_pattern(self, group_id: Union[str, int]):
        """Delete regex config for a group."""
        patterns = self._get_group_patterns()
        gid = str(group_id)
        if gid in patterns:
            del patterns[gid]
            self._save_group_patterns(patterns)

    def set_group_match_by_tvg_id(self, group_id: Union[str, int], enabled: bool):
        """Enable or disable TVG-ID matching for a group."""
        patterns = self._get_group_patterns()
        gid = str(group_id)
        existing = patterns.get(gid, {}) if isinstance(patterns.get(gid), dict) else {}
        patterns[gid] = {
            "name": existing.get("name", ""),
            "enabled": existing.get("enabled", True),
            "match_by_tvg_id": bool(enabled),
            "regex_patterns": existing.get("regex_patterns", [])
        }
        self._save_group_patterns(patterns)

    def get_group_match_config(self, group_id: Union[str, int]) -> Dict[str, Any]:
        """Get matching config for a group."""
        cfg = self.get_group_pattern(group_id) or {}
        return {
            "match_by_tvg_id": cfg.get("match_by_tvg_id", False),
            "enabled": cfg.get("enabled", True),
            "name": cfg.get("name", ""),
            "regex_patterns": cfg.get("regex_patterns", [])
        }
    
    def validate_regex_patterns(self, patterns: List[str]) -> Tuple[bool, Optional[str]]:
        """Validate a list of regex patterns.
        
        Args:
            patterns: List of regex pattern strings to validate
            
        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        if not patterns:
            return False, "At least one regex pattern is required"
        
        for pattern in patterns:
            if not pattern or not isinstance(pattern, str):
                return False, f"Pattern must be a non-empty string"
            
            try:
                # Temporarily substitute CHANNEL_NAME with a placeholder for validation
                # Use a simple placeholder that won't interfere with regex syntax
                validation_pattern = pattern.replace('CHANNEL_NAME', _CHANNEL_NAME_PLACEHOLDER)
                re.compile(validation_pattern)
            except re.error as e:
                return False, f"Invalid regex pattern '{pattern}': {str(e)}"
        
        return True, None
    
    def add_channel_pattern(self, channel_id: str, name: str, regex_patterns: 'Union[List[str], List[Dict]]', enabled: bool = True, m3u_accounts: Optional[List[int]] = None, silent: bool = False):
        """Add or update a channel pattern.
        
        Args:
            channel_id: Channel ID
            name: Channel name
            regex_patterns: Can be either:
                          - List[str]: Legacy format, list of regex pattern strings
                          - List[Dict]: New format with per-pattern m3u_accounts
                              [{"pattern": str, "m3u_accounts": List[int] | None}, ...]
            enabled: Whether the pattern is enabled
            m3u_accounts: Optional list of M3U account IDs (legacy, channel-level).
                         Only used when regex_patterns is List[str].
                         Examples:
                         - None: Field not stored, applies to all M3U accounts (backward compatible)
                         - []: Empty list stored, explicitly means "all M3U accounts"
                         - [1, 2, 3]: Only match streams from M3U accounts with these IDs
            silent: If True, log at DEBUG level instead of INFO (useful for batch operations)
            
        Raises:
            ValueError: If any regex pattern is invalid
        """
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        # Normalize regex_patterns to new format
        normalized_patterns = []
        
        if isinstance(regex_patterns, list) and len(regex_patterns) > 0:
            if isinstance(regex_patterns[0], dict):
                # New format: List[Dict]
                for item in regex_patterns:
                    if not isinstance(item, dict) or "pattern" not in item:
                        raise ValueError("Each pattern object must have a 'pattern' field")
                    normalized_patterns.append({
                        "pattern": item["pattern"],
                        "m3u_accounts": item.get("m3u_accounts")
                    })
            else:
                # Legacy format: List[str] - convert to new format
                for pattern in regex_patterns:
                    normalized_patterns.append({
                        "pattern": pattern,
                        "m3u_accounts": m3u_accounts  # Use channel-level m3u_accounts for all patterns
                    })
        else:
            raise ValueError("At least one regex pattern is required")
        
        # Validate patterns
        pattern_strings = [p["pattern"] for p in normalized_patterns]
        is_valid, error_msg = self.validate_regex_patterns(pattern_strings)
        if not is_valid:
            raise ValueError(error_msg)

        # Preserve existing match_by_tvg_id flag if already set
        existing_cfg = self.channel_patterns.get('patterns', {}).get(str(channel_id), {})
        match_by_tvg_id = existing_cfg.get('match_by_tvg_id', False)
        
        db.upsert_channel_regex_config(
            channel_id=str(channel_id),
            name=name,
            enabled=enabled,
            match_by_tvg_id=match_by_tvg_id,
            regex_patterns=normalized_patterns,
        )

        # Update in-memory cache
        with self.lock:
            self.channel_patterns.setdefault('patterns', {})[str(channel_id)] = {
                'name': name,
                'enabled': enabled,
                'match_by_tvg_id': match_by_tvg_id,
                'regex': [p['pattern'] for p in normalized_patterns],
                'regex_patterns': normalized_patterns,
            }
            if self._config_file is not None and self._file_backed_compat:
                self._config_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._config_file, 'w', encoding='utf-8') as fh:
                    json.dump(self.channel_patterns, fh, indent=2)
                self._remember_config_file_signature()
        self._clear_runtime_caches()
        
        if silent:
            logger.debug(f"Added/updated {len(normalized_patterns)} pattern(s) for channel {channel_id}: {name}")
        else:
            logger.info(f"Added/updated {len(normalized_patterns)} pattern(s) for channel {channel_id}: {name}")
    
    def delete_channel_pattern(self, channel_id: str):
        """Delete all regex patterns for a channel."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        channel_id = str(channel_id)
        if channel_id in self.channel_patterns.get("patterns", {}):
            with self.lock:
                del self.channel_patterns["patterns"][channel_id]
            self._clear_runtime_caches()
            db.delete_channel_regex_config(str(channel_id))
            logger.info(f"Deleted all patterns for channel {channel_id}")
        else:
            logger.warning(f"No patterns found for channel {channel_id}")
    
    def reload_patterns(self):
        """Reload patterns from SQL (refreshes the in-memory cache)."""
        if self._manual_regex_config:
            return

        if self._config_file_changed_since_last_import():
            self._seed_from_config_file(self._config_file)
            self._remember_config_file_signature()

        with self.lock:
            self.channel_patterns = self._load_patterns()
            self.group_patterns = self._load_group_patterns()
        self._clear_runtime_caches()
        logger.debug("Reloaded regex patterns from SQL")
    
    def _substitute_channel_variables(self, pattern: str, channel_name: str) -> str:
        """Substitute channel name variables in a regex pattern.
        
        Args:
            pattern: Regex pattern that may contain CHANNEL_NAME
            channel_name: Name of the channel to substitute
            
        Returns:
            Pattern with variables substituted
        """
        # Replace CHANNEL_NAME with the actual channel name
        # Escape special regex characters in channel name to avoid issues
        escaped_channel_name = re.escape(channel_name)
        return pattern.replace('CHANNEL_NAME', escaped_channel_name)
    
    def match_stream_to_channels(self, stream_name: str, stream_m3u_account: Optional[int] = None,
                               stream_tvg_id: Optional[str] = None, channel_tvg_ids: Optional[Dict[str, str]] = None,
                               channel_match_priorities: Optional[Dict[str, List[str]]] = None,
                               channel_to_group_map: Optional[Dict[str, Any]] = None,
                               channel_name_map: Optional[Dict[str, str]] = None) -> List[str]:
        """
        Match a stream name and optionally TVG-ID to channels using regex patterns and TVG-ID matching.
        
        Args:
            stream_name: The name of the stream to match
            stream_m3u_account: The ID of the M3U account the stream belongs to (optional)
            stream_tvg_id: The TVG-ID of the stream (optional)
            channel_tvg_ids: Dictionary mapping channel_id -> tvg_id (optional, for optimization)
            channel_match_priorities: Dictionary mapping channel_id -> ['tvg', 'regex'] or ['regex', 'tvg'] (optional)
            
        Returns:
            List of channel IDs that match the stream
        """
        matches = []
        case_sensitive = self.channel_patterns.get("global_settings", {}).get("case_sensitive", True)
        
        search_name = stream_name
        
        channel_to_group_map = channel_to_group_map or {}
        channel_name_map = channel_name_map or {}

        # Iterate all target channels when available so group-only configs are considered.
        explicit_channel_ids = set(self.channel_patterns.get("patterns", {}).keys())
        if channel_tvg_ids:
            explicit_channel_ids.update(str(cid) for cid in channel_tvg_ids.keys())
        if channel_to_group_map:
            explicit_channel_ids.update(str(cid) for cid in channel_to_group_map.keys())

        for channel_id in explicit_channel_ids:
            group_id = channel_to_group_map.get(str(channel_id))
            config = self._get_effective_channel_config(channel_id, group_id)
            if not isinstance(config, dict) or not config:
                continue
            
            # Determine priority order
            # Default: TVG first (if enabled)
            priority_order = ['tvg', 'regex']
            if channel_match_priorities:
                # Look up by string ID since config keys are stringified
                mapped_order = channel_match_priorities.get(str(channel_id))
                if mapped_order:
                    priority_order = mapped_order
            
            matched_channel = False
            
            # Check based on priority order
            # We stop checking a channel if one method matches (optimization and precedence)
            for match_type in priority_order:
                if match_type == 'tvg':
                    # Check for TVG-ID match if enabled and not already matched
                    if not matched_channel and stream_tvg_id and channel_tvg_ids and config.get("match_by_tvg_id", False):
                        channel_tvg_id = channel_tvg_ids.get(str(channel_id))
                        if channel_tvg_id and stream_tvg_id == channel_tvg_id:
                            matches.append(channel_id)
                            matched_channel = True
                            break # Skip other match types for this channel
                            
                elif match_type == 'regex':
                    if matched_channel:
                        continue
                    
                    if not config.get("enabled", True):
                        continue
                    
                    channel_name = channel_name_map.get(str(channel_id)) or config.get("name", "")
                    match_by_tvg = config.get("match_by_tvg_id", False)
                    
                    # Support both new format (regex_patterns) and old format (regex) for backward compatibility
                    regex_patterns = config.get("regex_patterns")
                    if regex_patterns is None:
                        # Fallback to old format
                        old_regex = config.get("regex", [])
                        old_m3u_accounts = config.get("m3u_accounts")
                        regex_patterns = [{"pattern": p, "m3u_accounts": old_m3u_accounts} for p in old_regex]
                    
                    regex_matched = False
                    for pattern_obj in regex_patterns:
                        # Handle both dict and string patterns for flexibility
                        if isinstance(pattern_obj, dict):
                            pattern = pattern_obj.get("pattern", "")
                            pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        else:
                            # Legacy string format
                            pattern = pattern_obj
                            pattern_m3u_accounts = None
                        
                        if not pattern:
                            continue
                        
                        # Check if this regex pattern applies to the stream's M3U account
                        if pattern_m3u_accounts is not None and len(pattern_m3u_accounts) > 0:
                            # Pattern is limited to specific M3U accounts
                            if stream_m3u_account is None or stream_m3u_account not in pattern_m3u_accounts:
                                # Stream's M3U account is not in the allowed list, skip this pattern
                                continue
                        
                        # SAFETY CHECK: If match_by_tvg_id is enabled, IGNORE catch-all regexes
                        # This prevents the issue where a lingering ".*" causes unwanted matches
                        # despite the user enabling TVG matching.
                        if match_by_tvg:
                            is_catch_all = pattern == ".*" or pattern == "^.*$" or pattern == ".+" or pattern == "^.+$"
                            if is_catch_all:
                                # logger.debug(f"Ignoring catch-all regex '{pattern}' for channel {channel_id} because match_by_tvg_id is enabled")
                                continue
                        
                        try:
                            compiled_pattern = _compile_stream_search_regex(pattern, channel_name, case_sensitive)
                            if compiled_pattern.search(search_name):
                                matches.append(channel_id)
                                matched_channel = True
                                regex_matched = True
                                # logger.debug(f"Stream '{stream_name}' matched channel {channel_id} with pattern '{pattern}'")
                                break  # Only match once per channel
                        except re.error as e:
                            logger.error(f"Invalid regex pattern '{pattern}' for channel {channel_id}: {e}")
                    
                    if regex_matched:
                        break  # Skip other match types for this channel
        
        return matches
    
    def match_stream_to_channels_with_priority(self, stream_name: str, stream_m3u_account: Optional[int] = None,
                                             stream_tvg_id: Optional[str] = None, channel_tvg_ids: Optional[Dict[str, str]] = None,
                                             channel_match_priorities: Optional[Dict[str, List[str]]] = None,
                                             channel_to_group_map: Optional[Dict[str, Any]] = None,
                                             channel_name_map: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Match a stream name and optionally TVG-ID to channels using regex patterns with priority.
        
        Args:
            stream_name: The name of the stream to match
            stream_m3u_account: The ID of the M3U account the stream belongs to (optional)
            stream_tvg_id: The TVG-ID of the stream (optional)
            channel_tvg_ids: Dictionary mapping channel_id -> tvg_id (optional)
            channel_match_priorities: Dictionary mapping channel_id -> priority order list (optional)
            
        Returns:
            List of dictionaries containing channel_id and priority
        """
        matches = []
        case_sensitive = self.channel_patterns.get("global_settings", {}).get("case_sensitive", True)
        
        search_name = stream_name
        
        channel_to_group_map = channel_to_group_map or {}
        channel_name_map = channel_name_map or {}

        with self.lock:
            explicit_channel_ids = set(self.channel_patterns.get("patterns", {}).keys())
            if channel_tvg_ids:
                explicit_channel_ids.update(str(cid) for cid in channel_tvg_ids.keys())
            if channel_to_group_map:
                explicit_channel_ids.update(str(cid) for cid in channel_to_group_map.keys())

            for channel_id in explicit_channel_ids:
                group_id = channel_to_group_map.get(str(channel_id))
                config = self._get_effective_channel_config(channel_id, group_id)
                if not isinstance(config, dict) or not config:
                    continue
                
                matched = False
                match_source = "regex"
                
                # Determine priority order
                priority_order = ['tvg', 'regex']
                if channel_match_priorities:
                    mapped_order = channel_match_priorities.get(str(channel_id))
                    if mapped_order:
                        priority_order = mapped_order
                        
                for match_type in priority_order:
                    if match_type == 'tvg':
                        # Check for TVG-ID match if enabled and not already matched
                        if not matched and stream_tvg_id and channel_tvg_ids and config.get("match_by_tvg_id", False):
                            channel_tvg_id = channel_tvg_ids.get(str(channel_id))
                            if channel_tvg_id and stream_tvg_id == channel_tvg_id:
                                matched = True
                                match_source = "tvg_id"
                                priority = 1000
                                break
                                
                    elif match_type == 'regex':
                        if matched:
                            continue
                            
                        if not config.get("enabled", True):
                            continue
                        
                        channel_name = channel_name_map.get(str(channel_id)) or config.get("name", "")
                        match_by_tvg = config.get("match_by_tvg_id", False)
                        
                        # Support both new format (regex_patterns) and old format (regex) for backward compatibility
                        regex_patterns = config.get("regex_patterns")
                        if regex_patterns is None:
                            # Fallback to old format
                            old_regex = config.get("regex", [])
                            old_m3u_accounts = config.get("m3u_accounts")
                            regex_patterns = [{"pattern": p, "m3u_accounts": old_m3u_accounts} for p in old_regex]
                        
                        regex_matched = False
                        best_regex_priority = 0
                        
                        for pattern_obj in regex_patterns:
                            # Handle both dict and string patterns for flexibility
                            if isinstance(pattern_obj, dict):
                                pattern = pattern_obj.get("pattern", "")
                                pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                                pattern_priority = pattern_obj.get("priority", 0)
                            else:
                                # Legacy string format
                                pattern = pattern_obj
                                pattern_m3u_accounts = None
                                pattern_priority = 0
                            
                            if not pattern:
                                continue
                            
                            # Check if this regex pattern applies to the stream's M3U account
                            if pattern_m3u_accounts is not None and len(pattern_m3u_accounts) > 0:
                                # Pattern is limited to specific M3U accounts
                                if stream_m3u_account is None or stream_m3u_account not in pattern_m3u_accounts:
                                    # Stream's M3U account is not in the allowed list, skip this pattern
                                    continue
                            
                            # SAFETY CHECK: If match_by_tvg_id is enabled, IGNORE catch-all regexes
                            if match_by_tvg:
                                is_catch_all = pattern == ".*" or pattern == "^.*$" or pattern == ".+" or pattern == "^.+$"
                                if is_catch_all:
                                    continue

                            try:
                                compiled_pattern = _compile_stream_search_regex(pattern, channel_name, case_sensitive)
                            except re.error:
                                continue

                            if compiled_pattern.search(search_name):
                                regex_matched = True
                                best_regex_priority = pattern_priority
                                # Only match once per channel
                                break
                                
                        if regex_matched:
                            matched = True
                            match_source = "regex"
                            priority = best_regex_priority
                            break

            
                if matched:
                    matches.append({
                        "channel_id": channel_id,
                        "priority": priority,
                        "source": match_source
                    })
    
        return matches

    def set_match_by_tvg_id(self, channel_id: Union[str, int], enabled: bool) -> bool:
        """Enable or disable matching by TVG-ID for a channel."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        with self.lock:
            channel_id = str(channel_id)
            if "patterns" not in self.channel_patterns:
                self.channel_patterns["patterns"] = {}
                
            if channel_id not in self.channel_patterns["patterns"]:
                self.channel_patterns["patterns"][channel_id] = {
                    "regex_patterns": [],
                    "match_by_tvg_id": enabled,
                    "name": "",
                    "enabled": True,
                }
            else:
                self.channel_patterns["patterns"][channel_id]["match_by_tvg_id"] = enabled

        db.update_channel_regex_tvg_id(str(channel_id), enabled)
        return True

    def get_match_by_tvg_id(self, channel_id: Union[str, int], group_id: Optional[Union[str, int]] = None) -> bool:
        """Check if matching by TVG-ID is enabled for a channel."""
        with self.lock:
            effective = self._get_effective_channel_config(channel_id, group_id)
            if isinstance(effective, dict):
                return effective.get("match_by_tvg_id", False)
            return False
    
    def get_patterns(self) -> Dict:
        """Get current patterns configuration (in-memory snapshot)."""
        return self.channel_patterns
    
    def has_regex_patterns(self, channel_id: str, group_id: Optional[Union[str, int]] = None) -> bool:
        """Check if a channel has regex patterns configured and enabled.
        
        A channel is considered to have regex patterns if:
        1. The channel exists in the patterns configuration
        2. The pattern configuration is enabled (enabled=True)
        3. The regex list is non-empty
        
        Args:
            channel_id: Channel ID to check
            
        Returns:
            True if the channel has at least one enabled regex pattern, False otherwise
        """
        channel_config = self._get_effective_channel_config(channel_id, group_id)
        if not channel_config:
            return False
        
        # Check if the pattern is enabled
        if not channel_config.get("enabled", True):
            return False
        
        # Check if there are any regex patterns (support both old and new format)
        regex_patterns = channel_config.get("regex_patterns")
        if regex_patterns is None:
            # Fallback to old format
            regex_patterns = channel_config.get("regex", [])
        
        return isinstance(regex_patterns, list) and len(regex_patterns) > 0
    
    def get_channel_regex_filter(self, channel_id: str, default: str = ".*", group_id: Optional[Union[str, int]] = None) -> Optional[str]:
        """Get the combined regex filter for a channel for stream name matching.
        
        Combines all enabled regex patterns for the channel into a single OR pattern.
        Returns `default` (standard ".*") if no patterns are configured or channel is disabled.
        
        Args:
            channel_id: Channel ID to get regex filter for
            default: Default value to return if no patterns found (default: ".*")
            
        Returns:
            Combined regex pattern string (e.g., '(pattern1|pattern2|pattern3)')
        """
        channel_config = self._get_effective_channel_config(channel_id, group_id)
        if not channel_config:
            return default
        
        # Check if the pattern is enabled
        if not channel_config.get("enabled", True):
            return default
        
        # Get regex patterns (support both old and new format)
        regex_patterns = channel_config.get("regex_patterns")
        if regex_patterns is None:
            # Fallback to old format
            regex_patterns = channel_config.get("regex", [])
        
        if not isinstance(regex_patterns, list) or len(regex_patterns) == 0:
            return default
        
        # Extract pattern strings from objects (new format) or use directly (old format)
        pattern_strings = []
        for pattern_obj in regex_patterns:
            if isinstance(pattern_obj, dict):
                pattern = pattern_obj.get('pattern', '')
            else:
                # Legacy format
                pattern = pattern_obj
            
            if pattern and isinstance(pattern, str):
                pattern_strings.append(pattern)
        
        if not pattern_strings:
            return default
        
        # If only one pattern, return it directly
        if len(pattern_strings) == 1:
            return pattern_strings[0]
        
        # Combine multiple patterns with OR
        # Each pattern is wrapped in a non-capturing group for safety
        combined = '|'.join(f'(?:{p})' for p in pattern_strings)
        return f'({combined})'

    def get_channel_match_config(self, channel_id: str, group_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Get the matching configuration for a channel.
        
        Args:
            channel_id: Channel ID
            
        Returns:
            Dictionary with matching configuration (match_by_tvg_id, enabled, etc.)
        """
        channel_config = self._get_effective_channel_config(channel_id, group_id) or {}
        return {
            "match_by_tvg_id": channel_config.get("match_by_tvg_id", False),
            "enabled": channel_config.get("enabled", True),
            "name": channel_config.get("name", "")
        }


class AutomatedStreamManager:
    """Main automated stream management system."""

    M3U_REFRESH_WAIT_DEFAULTS = {
        "enabled": True,
        "timeout_seconds": 600,
        "poll_interval_seconds": 10,
        "stable_polls_required": 2,
        "min_wait_seconds": 0,
        "retry_failed_providers": False,
    }

    RUN_STAGES = [
        ("settings", "Preparing"),
        ("period_discovery", "Schedule"),
        ("m3u_refresh", "M3U Refresh"),
        ("cache_sync", "Cache Sync"),
        ("stream_matching", "Matching"),
        ("quality_queueing", "Queueing"),
        ("quality_checking", "Quality Check"),
        ("finalizing", "Finalizing"),
    ]

    RUN_SNAPSHOT_HISTORY_KEY = "streamflow_run_snapshots"
    RUN_SNAPSHOT_SETTINGS_KEY = "streamflow_run_snapshot_settings"
    RUN_SNAPSHOT_DEFAULT_MAX_BYTES = 50 * 1024
    RUN_SNAPSHOT_DEFAULT_RETENTION = 50
    MANUAL_STOP_REQUEST_KEY = "automation_manual_stop_request"
    
    def __init__(self, config_file=None):
        self._explicit_config_file = config_file is not None
        if config_file is None:
            config_file = CONFIG_DIR / "automation_config.json"
        self.config_file = Path(config_file)
        self.config = self._load_config()
        self.changelog = ChangelogManager()
        self.regex_matcher = RegexChannelMatcher()
        self.channel_visibility_automation = ChannelVisibilityAutomation()
        
        # Initialize dead streams tracker
        self.dead_streams_tracker = None
        if DEAD_STREAMS_TRACKER_AVAILABLE:
            try:
                self.dead_streams_tracker = DeadStreamsTracker()
                logger.info("Dead streams tracker initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize dead streams tracker: {e}")
        
        self.running = False
        self.state_file = CONFIG_DIR / "automation_state.json"
        self.last_playlist_update = None
        self._period_skip_history = {}
        self.period_last_run = self._load_state()  # Tracks last run time per period ID
        self.automation_start_time = None
        
        # Cache for M3U accounts to avoid redundant API calls within a single automation cycle
        # This is cleared after each cycle completes
        self._m3u_accounts_cache = None
        
        # Background thread management
        self.automation_thread = None
        self.automation_running = False
        self.automation_wake_event = threading.Event()
        self._manual_stop_requested = threading.Event()
        self.force_next_run = False
        self.forced_period_id = None
        
        # Lock to prevent concurrent execution of heavy batch processes
        self._lock = threading.Lock()

        self._run_status_lock = threading.RLock()
        self._run_sequence = 0
        self._run_status = self._build_run_status(
            run_id=None,
            state="idle",
            stage="idle",
            stage_label="Idle",
            message="No automation cycle has run yet",
        )
    
    def _load_state(self) -> Dict[str, datetime]:
        """Load persisted automation state from file."""
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        state = None
        try:
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_state').first()
            if setting and setting.value:
                state = setting.value
            else:
                raise FileNotFoundError("No automation state found in DB")
            
            # Convert stored ISO strings back to datetime objects
            loaded_runs = state.get('period_last_run', {})
            parsed_runs = {}
            for pid, iso_str in loaded_runs.items():
                try:
                    parsed_runs[pid] = datetime.fromisoformat(iso_str)
                except (ValueError, TypeError):
                    pass

            loaded_skip_history = state.get('period_skip_history', {})
            parsed_skip_history = {}
            if isinstance(loaded_skip_history, dict):
                for pid, entries in loaded_skip_history.items():
                    if not isinstance(entries, list):
                        continue
                    normalized_entries = []
                    for entry in entries[:10]:
                        if isinstance(entry, dict):
                            normalized_entries.append(dict(entry))
                    if normalized_entries:
                        parsed_skip_history[str(pid)] = normalized_entries
            self._period_skip_history = parsed_skip_history
            
            if parsed_runs:
                logger.info(f"Loaded {len(parsed_runs)} period last-run timestamps from state file")
            return parsed_runs
        except (Exception):
            logger.warning(f"Could not load state from DB, starting fresh")
        finally:
            try: session.close()
            except: pass
        return {}

    def _save_state(self):
        """Save current automation state to SQL."""
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        try:
            # Convert datetime objects to ISO strings for JSON serialization
            serializable_runs = {
                pid: dt.isoformat() 
                for pid, dt in self.period_last_run.items() 
                if isinstance(dt, datetime)
            }
            state = {
                'period_last_run': serializable_runs,
                'period_skip_history': getattr(self, '_period_skip_history', {}),
            }
            
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_state').first()
            if not setting:
                setting = SystemSetting(key='automation_state', value=state)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = state
                flag_modified(setting, "value")
            session.commit()
            session.close()
        except Exception as e:
            logger.error(f"Failed to save automation state: {e}")

    
    def _load_config(self) -> Dict:
        """Load automation configuration from SQL."""
        default_config = {
            "playlist_update_interval_minutes": 5,
            "playlist_update_cron": "",
            "enabled_m3u_accounts": [],
            "autostart_automation": False,
            "enabled_features": {
                "auto_playlist_update": True,
                "auto_stream_discovery": True,
                "changelog_tracking": True
            },
            "m3u_refresh_wait": dict(self.M3U_REFRESH_WAIT_DEFAULTS),
            "validate_existing_streams": False,
            "verify_stream_assignments": False
        }

        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as fh:
                    file_config = json.load(fh) or {}
                return {**default_config, **file_config}
            except Exception as exc:
                logger.warning(f"Could not load automation config file {self.config_file}: {exc}")

        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        try:
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_config').first()
            if setting and setting.value:
                return setting.value
        except Exception as e:
            logger.error(f"Failed to load automation config: {e}")
        finally:
            try: session.close()
            except: pass
        
        self._save_config(default_config)
        return default_config
    
    def _save_config(self, config: Dict):
        """Save configuration to SQL."""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as fh:
                json.dump(config, fh, indent=2)
        except Exception as file_exc:
            logger.debug(f"Could not write automation config file {self.config_file}: {file_exc}")

        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        try:
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_config').first()
            if not setting:
                setting = SystemSetting(key='automation_config', value=config)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = config
                flag_modified(setting, "value")
            session.commit()
        except Exception as e:
            logger.error(f"Failed to save automation config: {e}")
            if 'session' in locals():
                session.rollback()
        finally:
            if 'session' in locals():
                session.close()
    
    def update_config(self, updates: Dict):
        """Update configuration with new values and apply immediately."""
        # Log what's being updated
        config_changes = []
        
        if 'playlist_update_interval_minutes' in updates:
            old_interval = self.config.get('playlist_update_interval_minutes', 5)
            new_interval = updates['playlist_update_interval_minutes']
            if old_interval != new_interval:
                config_changes.append(f"Playlist update interval: {old_interval} → {new_interval} minutes")
        
        if 'enabled_features' in updates:
            old_features = self.config.get('enabled_features', {})
            new_features = updates['enabled_features']
            for feature, enabled in new_features.items():
                old_value = old_features.get(feature, True)
                if old_value != enabled:
                    status = "enabled" if enabled else "disabled"
                    config_changes.append(f"{feature}: {status}")
        
        if 'enabled_m3u_accounts' in updates:
            old_accounts = self.config.get('enabled_m3u_accounts', [])
            new_accounts = updates['enabled_m3u_accounts']
            if old_accounts != new_accounts:
                if not new_accounts:
                    config_changes.append("M3U accounts: all enabled")
                else:
                    config_changes.append(f"M3U accounts: {len(new_accounts)} selected")
        
        # Apply the configuration update
        self.config.update(updates)
        self._save_config(self.config)
        
        # Log the changes
        if config_changes:
            logger.info(f"Automation configuration updated: {'; '.join(config_changes)}")
            logger.info("Changes will take effect on next scheduled operation")
        else:
            logger.info("Automation configuration updated")

    def _build_run_status(
        self,
        *,
        run_id: Optional[str],
        state: str,
        stage: str,
        stage_label: str,
        message: str = "",
        forced: bool = False,
        forced_period_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.now().isoformat()
        return {
            "run_id": run_id,
            "state": state,
            "status": state,
            "active": state == "running",
            "stage": stage,
            "stage_label": stage_label,
            "message": message,
            "forced": forced,
            "forced_period_id": forced_period_id,
            "started_at": now if state == "running" else None,
            "stage_started_at": now if state == "running" else None,
            "updated_at": now,
            "completed_at": None,
            "duration_seconds": None,
            "stage_duration_seconds": None,
            "elapsed_seconds": 0,
            "stage_elapsed_seconds": 0,
            "current": 0,
            "total": None,
            "percent": 0,
            "progress": {
                "current": 0,
                "total": None,
                "percent": 0,
                "message": "",
            },
            "stages": [
                {
                    "key": key,
                    "label": label,
                    "status": "running" if key == stage and state == "running" else "pending",
                    "current": 0,
                    "total": None,
                    "percent": 0,
                    "message": message if key == stage else "",
                }
                for key, label in self.RUN_STAGES
            ],
            "counts": {},
            "durations": {},
            "last_error": None,
        }

    @staticmethod
    def _coerce_positive_int(value: Any, default: int, *, min_value: int = 1, max_value: int = 1000) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        if number < min_value or number > max_value:
            return default
        return number

    def _run_snapshot_limits(self) -> Dict[str, int]:
        settings = {}
        try:
            from apps.database.manager import get_db_manager

            settings = get_db_manager().get_system_setting(self.RUN_SNAPSHOT_SETTINGS_KEY, {}) or {}
        except Exception as exc:
            logger.debug("Could not load run snapshot settings: %s", exc)
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        return {
            "max_bytes": self._coerce_positive_int(
                settings.get("max_bytes"),
                self.RUN_SNAPSHOT_DEFAULT_MAX_BYTES,
                min_value=1024,
                max_value=512 * 1024,
            ),
            "retention_count": self._coerce_positive_int(
                settings.get("retention_count"),
                self.RUN_SNAPSHOT_DEFAULT_RETENTION,
                min_value=1,
                max_value=500,
            ),
        }

    @staticmethod
    def _snapshot_size_bytes(snapshot: Dict[str, Any]) -> int:
        try:
            return len(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return 0

    def _bound_run_snapshot(self, snapshot: Dict[str, Any], *, max_bytes: Optional[int] = None) -> Dict[str, Any]:
        limits = self._run_snapshot_limits()
        byte_limit = max_bytes or limits["max_bytes"]
        bounded = copy.deepcopy(snapshot or {})
        if self._snapshot_size_bytes(bounded) <= byte_limit:
            bounded["snapshot_size_bytes"] = self._snapshot_size_bytes(bounded)
            bounded["snapshot_truncated"] = False
            return bounded

        for key in ("effective_profiles", "quality_rules", "feature_flags", "dispatcharr_status", "teamarr_status", "stale_warnings"):
            value = bounded.get(key)
            if isinstance(value, list):
                bounded[f"{key}_omitted_count"] = max(0, len(value) - 5)
                bounded[key] = value[:5]
            elif isinstance(value, dict):
                bounded[f"{key}_omitted"] = True
                bounded[key] = {}
            bounded["snapshot_truncated"] = True
            if self._snapshot_size_bytes(bounded) <= byte_limit:
                bounded["snapshot_size_bytes"] = self._snapshot_size_bytes(bounded)
                return bounded

        minimal = {
            "schema_version": bounded.get("schema_version", 1),
            "run_id": bounded.get("run_id"),
            "run_mode": bounded.get("run_mode"),
            "start_source": bounded.get("start_source"),
            "started_at": bounded.get("started_at"),
            "streamflow_version": bounded.get("streamflow_version"),
            "streamflow_commit": bounded.get("streamflow_commit"),
            "snapshot_truncated": True,
            "snapshot_omitted_reason": "max_bytes_exceeded",
        }
        minimal["snapshot_size_bytes"] = self._snapshot_size_bytes(minimal)
        return minimal

    @staticmethod
    def _streamflow_version_context() -> Dict[str, Optional[str]]:
        version = os.getenv("STREAMFLOW_VERSION")
        if not version:
            current_file = Path(__file__)
            for version_file in (
                current_file.parent / "version.txt",
                current_file.parents[2] / "version.txt",
                current_file.parents[2] / "static" / "version.txt",
            ):
                try:
                    if version_file.exists():
                        value = version_file.read_text(encoding="utf-8").strip()
                        if value:
                            version = value
                            break
                except Exception:
                    continue
        commit = (
            os.getenv("STREAMFLOW_COMMIT")
            or os.getenv("STREAMFLOW_REVISION")
            or os.getenv("GITHUB_SHA")
        )
        return {
            "version": version or "dev-unknown",
            "commit": commit or None,
        }

    @staticmethod
    def _run_mode_for_start(*, forced: bool, forced_period_id: Optional[str]) -> str:
        if forced_period_id:
            return "manual_period_run"
        if forced:
            return "manual_full_run"
        return "scheduler_run"

    def _safe_feature_flags(self, global_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        flags = {}
        config = getattr(self, "config", {}) if isinstance(getattr(self, "config", {}), dict) else {}
        enabled_features = config.get("enabled_features", {})
        if isinstance(enabled_features, dict):
            flags["enabled_features"] = {
                str(key): value
                for key, value in enabled_features.items()
                if isinstance(value, (bool, int, float, str, type(None)))
            }
        if isinstance(global_settings, dict):
            for key in (
                "regular_automation_enabled",
                "run_all_due_periods",
                "catch_up_cap",
                "automation_run_policy",
            ):
                value = global_settings.get(key)
                if isinstance(value, (bool, int, float, str, type(None))):
                    flags[key] = value
        return flags

    def _initial_run_snapshot(self, status: Dict[str, Any], *, forced: bool, forced_period_id: Optional[str]) -> Dict[str, Any]:
        version_context = self._streamflow_version_context()
        limits = self._run_snapshot_limits()
        snapshot = {
            "schema_version": 1,
            "run_id": status.get("run_id"),
            "run_mode": self._run_mode_for_start(forced=forced, forced_period_id=forced_period_id),
            "start_source": "manual" if forced or forced_period_id else "scheduler",
            "forced": bool(forced),
            "forced_period_id": forced_period_id,
            "started_at": status.get("started_at"),
            "streamflow_version": version_context["version"],
            "streamflow_commit": version_context["commit"],
            "feature_flags": self._safe_feature_flags(),
            "effective_profiles": [],
            "quality_rules": [],
            "capacity_profile_context": {
                "type": "not_discovered",
                "description": "Profile and account capacity is resolved after schedule discovery.",
            },
            "dispatcharr_status": {},
            "teamarr_status": {},
            "limits": limits,
        }
        return self._bound_run_snapshot(snapshot, max_bytes=limits["max_bytes"])

    def _profile_snapshot_items(
        self,
        active_periods: Dict[Tuple[str, str], Dict[str, Any]],
        automation_config: Any,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for (period_id, period_name), data in sorted(active_periods.items(), key=lambda item: str(item[0])):
            profile_id = data.get("profile_id")
            profile = automation_config.get_profile(profile_id) if profile_id else None
            stream_checking = profile.get("stream_checking", {}) if isinstance(profile, dict) else {}
            items.append({
                "period_id": period_id,
                "period_name": period_name,
                "profile_id": str(profile_id) if profile_id is not None else None,
                "profile_name": data.get("profile_name") or (profile.get("name") if isinstance(profile, dict) else None) or "Default",
                "channel_count": len(data.get("channels") or []),
                "quality_rules_enabled": bool(stream_checking.get("enabled", False)),
                "quality_rules_name": (profile.get("name") if isinstance(profile, dict) else None) or data.get("profile_name") or "Default",
                "check_all_streams": bool(stream_checking.get("check_all_streams", False)),
                "stream_limit": stream_checking.get("stream_limit", 0),
            })
        return items

    def _store_run_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        if not snapshot or not snapshot.get("run_id"):
            return
        limits = self._run_snapshot_limits()
        try:
            from apps.database.manager import get_db_manager

            db = get_db_manager()
            history = db.get_system_setting(self.RUN_SNAPSHOT_HISTORY_KEY, []) or []
            if not isinstance(history, list):
                history = []
            run_id = snapshot.get("run_id")
            history = [
                item for item in history
                if not isinstance(item, dict) or item.get("run_id") != run_id
            ]
            history.append(copy.deepcopy(snapshot))
            history = history[-limits["retention_count"]:]
            db.set_system_setting(self.RUN_SNAPSHOT_HISTORY_KEY, history)
        except Exception as exc:
            logger.debug("Could not store run snapshot: %s", exc)

    def _finalize_run_snapshot(
        self,
        active_periods: Dict[Tuple[str, str], Dict[str, Any]],
        automation_config: Any,
        global_settings: Optional[Dict[str, Any]],
        *,
        udi: Optional[Any] = None,
        teamarr_event_window: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._ensure_run_status_fields()
        with self._run_status_lock:
            if self._run_status.get("run_snapshot_finalized"):
                return
            base_snapshot = copy.deepcopy(self._run_status.get("run_snapshot") or {})
            started_at = self._run_status.get("started_at")
            run_id = self._run_status.get("run_id")

        profile_items = self._profile_snapshot_items(active_periods, automation_config)
        quality_rules = [
            {
                "profile_id": item.get("profile_id"),
                "profile_name": item.get("quality_rules_name"),
                "period_id": item.get("period_id"),
                "enabled": item.get("quality_rules_enabled"),
            }
            for item in profile_items
        ]
        channel_count = sum(item.get("channel_count", 0) for item in profile_items)
        dispatcharr_status = {}
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

        snapshot = {
            **base_snapshot,
            "run_id": run_id or base_snapshot.get("run_id"),
            "started_at": started_at or base_snapshot.get("started_at"),
            "effective_profiles": profile_items,
            "effective_profile_count": len(profile_items),
            "channel_count": channel_count,
            "quality_rules": quality_rules,
            "capacity_profile_context": {
                "type": "provider_account_profiles",
                "description": "Capacity is enforced by account limits and active provider profiles.",
                "profile_limited": any(item.get("quality_rules_enabled") for item in profile_items),
            },
            "feature_flags": self._safe_feature_flags(global_settings),
            "dispatcharr_status": dispatcharr_status,
            "stale_warnings": stale_warnings,
            "teamarr_status": {
                "event_window_active": bool(teamarr_event_window),
            },
        }
        bounded = self._bound_run_snapshot(snapshot)
        with self._run_status_lock:
            self._run_status["run_snapshot"] = bounded
            self._run_status["run_snapshot_finalized"] = True
        self._store_run_snapshot(bounded)

    @staticmethod
    def _summarize_channel_visibility_events(events: Optional[List[Dict[str, Any]]]) -> Dict[str, int]:
        hidden_channels = set()
        ready_channels = set()
        changed_events = 0
        for event in events or []:
            if not isinstance(event, dict) or not event.get("changed"):
                continue
            changed_events += 1
            channel_key = event.get("channel_id") or event.get("channel_ref")
            if channel_key in (None, ""):
                channel_key = f"event-{changed_events}"
            action = event.get("action")
            if action == "hidden":
                hidden_channels.add(str(channel_key))
            elif action == "unhidden":
                ready_channels.add(str(channel_key))
        return {
            "channels_hidden": len(hidden_channels),
            "channels_ready": len(ready_channels),
            "channel_visibility_changed": changed_events,
        }

    def _stage_label(self, stage_key: str) -> str:
        return dict(self.RUN_STAGES).get(stage_key, stage_key.replace("_", " ").title())

    def _ensure_run_status_fields(self) -> None:
        if not hasattr(self, "_run_status_lock"):
            self._run_status_lock = threading.RLock()
        if not hasattr(self, "_run_sequence"):
            self._run_sequence = 0
        if not hasattr(self, "_manual_stop_requested"):
            self._manual_stop_requested = threading.Event()
        if not hasattr(self, "_run_status"):
            self._run_status = self._build_run_status(
                run_id=None,
                state="idle",
                stage="idle",
                stage_label="Idle",
                message="No automation cycle has run yet",
            )

    def _start_run_status(self, *, forced: bool = False, forced_period_id: Optional[str] = None) -> None:
        self._ensure_run_status_fields()
        self._manual_stop_requested.clear()
        self._clear_persisted_manual_stop_request()
        with self._run_status_lock:
            self._run_sequence += 1
            run_id = f"automation-{int(time.time())}-{self._run_sequence}"
            self._run_status = self._build_run_status(
                run_id=run_id,
                state="running",
                stage="starting",
                stage_label="Starting",
                message="Preparing automation cycle",
                forced=forced,
                forced_period_id=forced_period_id,
            )
            self._run_status["run_snapshot"] = self._initial_run_snapshot(
                self._run_status,
                forced=forced,
                forced_period_id=forced_period_id,
            )
            self._run_status["run_snapshot_finalized"] = False

    def _update_run_status(
        self,
        *,
        stage: Optional[str] = None,
        stage_label: Optional[str] = None,
        message: Optional[str] = None,
        counts: Optional[Dict[str, Any]] = None,
        durations: Optional[Dict[str, Any]] = None,
        progress: Optional[Dict[str, Any]] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self._ensure_run_status_fields()
        with self._run_status_lock:
            status = self._run_status
            now = datetime.now()
            previous_stage = status.get("stage")
            if state:
                status["state"] = state
                status["status"] = state
                status["active"] = state == "running"
            if stage:
                status["stage"] = stage
                if stage != previous_stage:
                    status["stage_started_at"] = now.isoformat()
            if stage_label:
                status["stage_label"] = stage_label
            if message is not None:
                status["message"] = message
            if counts:
                status.setdefault("counts", {}).update(counts)
            if durations:
                normalized = {}
                for key, value in durations.items():
                    try:
                        normalized[key] = round(float(value), 3)
                    except (TypeError, ValueError):
                        normalized[key] = value
                status.setdefault("durations", {}).update(normalized)
            if progress:
                current = progress.get("current")
                total = progress.get("total")
                try:
                    percent = int((float(current) / float(total)) * 100) if total else progress.get("percent", 0)
                except (TypeError, ValueError, ZeroDivisionError):
                    percent = progress.get("percent", 0)
                status["progress"] = {
                    "current": current,
                    "total": total,
                    "percent": max(0, min(100, int(percent or 0))),
                    "message": progress.get("message", status.get("message", "")),
                }
            if error is not None:
                status["last_error"] = error
            status["updated_at"] = now.isoformat()

            stage_keys = [item["key"] for item in status.get("stages", [])]
            if stage in stage_keys:
                stage_index = stage_keys.index(stage)
                for idx, stage_item in enumerate(status.get("stages", [])):
                    if idx < stage_index and stage_item.get("status") in {"pending", "running"}:
                        stage_item.update({"status": "completed", "percent": 100})
                    elif idx == stage_index:
                        stage_item.update(
                            {
                                "status": "running" if status.get("state") == "running" else status.get("state", "running"),
                                "current": status.get("progress", {}).get("current", stage_item.get("current", 0)),
                                "total": status.get("progress", {}).get("total", stage_item.get("total")),
                                "percent": status.get("progress", {}).get("percent", stage_item.get("percent", 0)),
                                "message": status.get("progress", {}).get("message", status.get("message", "")),
                            }
                        )

            started_at = status.get("started_at")
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at)
                    status["duration_seconds"] = round((now - started).total_seconds(), 3)
                    status["elapsed_seconds"] = int((now - started).total_seconds())
                except (TypeError, ValueError):
                    pass
            stage_started_at = status.get("stage_started_at")
            if stage_started_at:
                try:
                    stage_started = datetime.fromisoformat(stage_started_at)
                    status["stage_duration_seconds"] = round((now - stage_started).total_seconds(), 3)
                    status["stage_elapsed_seconds"] = int((now - stage_started).total_seconds())
                except (TypeError, ValueError):
                    pass
    def _update_run_stage(
        self,
        stage_key: str,
        *,
        message: str = "",
        current: int = 0,
        total: Optional[int] = None,
        status: str = "running",
    ) -> None:
        stage_keys = [key for key, _label in self.RUN_STAGES]
        stage_index = stage_keys.index(stage_key) if stage_key in stage_keys else -1
        percent = int((current / total) * 100) if total else 0
        percent = max(0, min(100, percent))
        state = status if status not in {"running", "completed", "skipped"} else "running"

        self._update_run_status(
            stage=stage_key,
            stage_label=self._stage_label(stage_key),
            message=message,
            state=state,
        )

        with self._run_status_lock:
            self._run_status["current"] = current
            self._run_status["total"] = total
            self._run_status["percent"] = percent
            self._run_status["progress"] = {
                "current": current,
                "total": total,
                "percent": percent,
                "message": message,
            }
            for idx, stage in enumerate(self._run_status.get("stages", [])):
                if stage_index >= 0 and idx < stage_index and stage["status"] in {"pending", "running"}:
                    stage.update({"status": "completed", "percent": 100})
                elif idx == stage_index:
                    stage.update(
                        {
                            "status": status,
                            "current": current,
                            "total": total,
                            "percent": percent,
                            "message": message,
                        }
                    )

    def _update_run_progress(
        self,
        *,
        stage_key: Optional[str] = None,
        current: Optional[int] = None,
        total: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        self._ensure_run_status_fields()
        with self._run_status_lock:
            key = stage_key or self._run_status.get("stage")
            current_value = self._run_status.get("current", 0) if current is None else current
            total_value = self._run_status.get("total") if total is None else total
            percent = int((current_value / total_value) * 100) if total_value else self._run_status.get("percent", 0)
            percent = max(0, min(100, int(percent or 0)))

        self._update_run_status(
            stage=key,
            stage_label=self._stage_label(key),
            message=message,
        )

        with self._run_status_lock:
            self._run_status["current"] = current_value
            self._run_status["total"] = total_value
            self._run_status["percent"] = percent
            if message is not None:
                self._run_status["message"] = message
            self._run_status["progress"] = {
                "current": current_value,
                "total": total_value,
                "percent": percent,
                "message": message or self._run_status.get("message", ""),
            }
            for stage in self._run_status.get("stages", []):
                if stage["key"] == key:
                    stage.update(
                        {
                            "status": "running",
                            "current": current_value,
                            "total": total_value,
                            "percent": percent,
                        }
                    )
                    if message is not None:
                        stage["message"] = message
                    break

    def _sync_udi_cache_after_playlist_refresh(self, udi_manager: Any) -> bool:
        """Refresh UDI streams/channels while surfacing coarse run progress."""
        progress_total = 100
        all_success = True
        successful_steps = 0

        def update_stream_fetch_progress(payload: Dict[str, Any]) -> None:
            completed_pages = payload.get("completed_pages")
            total_pages = payload.get("total_pages")
            items_fetched = payload.get("items_fetched")
            expected_count = payload.get("expected_count")
            try:
                completed_pages_int = int(completed_pages)
                total_pages_int = int(total_pages)
                current = int((completed_pages_int / total_pages_int) * 80) if total_pages_int > 0 else 1
                current = max(1, min(80, current))
                page_detail = f" ({completed_pages_int}/{total_pages_int} pages"
                if items_fetched is not None and expected_count is not None:
                    page_detail += f", {items_fetched}/{expected_count} streams"
                page_detail += ")"
            except (TypeError, ValueError, ZeroDivisionError):
                current = 1
                page_detail = ""
            self._update_run_progress(
                stage_key="cache_sync",
                current=current,
                total=progress_total,
                message=f"Syncing stream cache{page_detail}",
            )

        def cache_sync_cancelled() -> bool:
            return self._is_manual_stop_requested()

        self._update_run_progress(
            stage_key="cache_sync",
            current=1,
            total=progress_total,
            message="Syncing stream cache",
        )
        try:
            streams_success = bool(
                udi_manager.refresh_streams(
                    progress_callback=update_stream_fetch_progress,
                    cancel_check=cache_sync_cancelled,
                )
            )
        except FetchCancelled:
            self._update_run_progress(
                stage_key="cache_sync",
                current=1,
                total=progress_total,
                message="Cache sync stopped by user",
            )
            raise
        except Exception as exc:
            logger.warning("UDI streams cache sync failed: %s", exc)
            streams_success = False
        if streams_success:
            successful_steps += 1
        all_success = all_success and streams_success
        self._update_run_progress(
            stage_key="cache_sync",
            current=80,
            total=progress_total,
            message=f"Syncing stream cache {'completed' if streams_success else 'reported warnings'}",
        )

        self._update_run_progress(
            stage_key="cache_sync",
            current=90,
            total=progress_total,
            message="Syncing channel cache",
        )
        try:
            channels_success = bool(udi_manager.refresh_channels())
        except Exception as exc:
            logger.warning("UDI channels cache sync failed: %s", exc)
            channels_success = False
        if channels_success:
            successful_steps += 1
        all_success = all_success and channels_success
        self._update_run_progress(
            stage_key="cache_sync",
            current=100,
            total=progress_total,
            message=f"Syncing channel cache {'completed' if channels_success else 'reported warnings'}",
        )

        self._update_run_status(
            counts={
                "cache_sync_successful_steps": successful_steps,
                "cache_sync_total_steps": 2,
                "cache_sync_state": "completed" if all_success else "warning",
            },
        )
        return all_success

    def _get_m3u_refresh_wait_config(self) -> Dict[str, Any]:
        raw_config = {}
        config = getattr(self, "config", {}) or {}
        if isinstance(config, dict):
            raw_config = config.get("m3u_refresh_wait") or {}
        if not isinstance(raw_config, dict):
            raw_config = {}

        merged = {**self.M3U_REFRESH_WAIT_DEFAULTS, **raw_config}

        def _positive_int(key: str, minimum: int) -> int:
            try:
                value = int(merged.get(key, self.M3U_REFRESH_WAIT_DEFAULTS[key]))
            except (TypeError, ValueError):
                value = self.M3U_REFRESH_WAIT_DEFAULTS[key]
            return max(minimum, value)

        return {
            "enabled": bool(merged.get("enabled", True)),
            "timeout_seconds": _positive_int("timeout_seconds", 30),
            "poll_interval_seconds": _positive_int("poll_interval_seconds", 1),
            "stable_polls_required": _positive_int("stable_polls_required", 1),
            "min_wait_seconds": _positive_int("min_wait_seconds", 0),
            "retry_failed_providers": bool(merged.get("retry_failed_providers", False)),
        }

    @staticmethod
    def _account_id_key(value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value).strip()

    @staticmethod
    def _safe_stream_count_from_udi(udi_manager: Any) -> Optional[int]:
        getter = getattr(udi_manager, "get_streams", None)
        if not callable(getter):
            return None
        try:
            return len(getter(log_result=False) or [])
        except TypeError:
            try:
                return len(getter() or [])
            except Exception:
                return None
        except Exception:
            return None

    @staticmethod
    def _account_is_refresh_busy(account: Dict[str, Any]) -> bool:
        busy_keys = {
            "is_refreshing",
            "refreshing",
            "is_updating",
            "updating",
            "processing",
            "in_progress",
            "task_running",
            "queued",
            "is_queued",
        }
        busy_statuses = {
            "building",
            "downloading",
            "fetching",
            "importing",
            "refreshing",
            "in_progress",
            "loading",
            "parsing",
            "pending",
            "preparing",
            "processing",
            "queued",
            "running",
            "started",
            "syncing",
            "updating",
        }

        for key in busy_keys:
            value = account.get(key)
            if value is True:
                return True
            if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1", "running"}:
                return True

        for key in ("status", "state", "refresh_status", "last_refresh_status"):
            value = account.get(key)
            if isinstance(value, str) and value.strip().lower() in busy_statuses:
                return True
        return False

    @staticmethod
    def _account_refresh_failed(account: Dict[str, Any]) -> bool:
        failed_statuses = {"failed", "error", "errored", "cancelled", "canceled"}
        for key in ("status", "state", "refresh_status", "last_refresh_status"):
            value = account.get(key)
            if isinstance(value, str) and value.strip().lower() in failed_statuses:
                return True
        return False

    def _build_m3u_refresh_monitor_snapshot(
        self,
        udi_manager: Any,
        target_account_ids: Optional[set],
    ) -> Dict[str, Any]:
        accounts_ok = False
        accounts: List[Dict[str, Any]] = []

        try:
            refresh_accounts = getattr(udi_manager, "refresh_m3u_accounts", None)
            accounts_ok = bool(refresh_accounts()) if callable(refresh_accounts) else False
        except Exception as exc:
            logger.debug("M3U refresh monitor account poll failed: %s", exc)

        try:
            getter = getattr(udi_manager, "get_m3u_accounts", None)
            raw_accounts = getter() if callable(getter) else []
            accounts = raw_accounts if isinstance(raw_accounts, list) else []
        except Exception as exc:
            logger.debug("M3U refresh monitor account read failed: %s", exc)
            accounts = []

        stream_count = self._safe_stream_count_from_udi(udi_manager)

        account_rows = []
        busy_count = 0
        failed_count = 0
        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_id = account.get("id")
            account_id_key = self._account_id_key(account_id)
            if target_account_ids and account_id_key not in target_account_ids:
                continue
            is_busy = self._account_is_refresh_busy(account)
            is_failed = self._account_refresh_failed(account)
            if is_busy:
                busy_count += 1
            if is_failed:
                failed_count += 1
            account_rows.append({
                "id": account_id,
                "name": account.get("name"),
                "status": account.get("status") or account.get("state") or account.get("refresh_status"),
                "last_refresh_status": account.get("last_refresh_status"),
                "busy": is_busy,
                "failed": is_failed,
                "updated_at": (
                    account.get("updated_at")
                    or account.get("last_updated")
                    or account.get("last_refresh")
                    or account.get("last_refreshed")
                    or account.get("last_refresh_at")
                ),
                "profile_count": len(account.get("profiles") or []) if isinstance(account.get("profiles"), list) else None,
            })

        signature = json.dumps(
            {
                "accounts": sorted(account_rows, key=lambda item: str(item.get("id"))),
                "stream_count": stream_count,
            },
            sort_keys=True,
            default=str,
        )

        return {
            "accounts_ok": accounts_ok,
            "streams_ok": False,
            "account_count": len(account_rows),
            "busy_count": busy_count,
            "failed_count": failed_count,
            "stream_count": stream_count,
            "accounts": account_rows,
            "failed_accounts": [account for account in account_rows if account.get("failed")],
            "signature": signature,
        }

    def _retry_failed_m3u_refresh_accounts(
        self,
        failed_accounts: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> int:
        retried = 0
        total = len(failed_accounts)
        for index, account in enumerate(failed_accounts, start=1):
            account_id = account.get("id")
            if account_id is None:
                continue
            account_name = account.get("name") or f"Account {account_id}"
            if progress_callback:
                progress_callback({
                    "state": "retrying_failed",
                    "current": index - 1,
                    "total": total,
                    "account_id": account_id,
                    "account_name": account_name,
                    "message": f"Retrying failed playlist {index}/{total}: {account_name}",
                    "wait_retry_count": retried,
                    "wait_failed_accounts": total,
                })
            try:
                response = refresh_m3u_playlists(account_id=int(account_id))
                if self._is_m3u_refresh_response_success(response):
                    retried += 1
                    if progress_callback:
                        progress_callback({
                            "state": "retrying_failed",
                            "current": index,
                            "total": total,
                            "account_id": account_id,
                            "account_name": account_name,
                            "message": f"Retry accepted for failed playlist {index}/{total}: {account_name}",
                            "wait_retry_count": retried,
                            "wait_failed_accounts": total,
                        })
                else:
                    logger.warning("Retry request for failed M3U account %s was not accepted", account_id)
            except Exception as exc:
                if self._is_m3u_refresh_already_running_error(exc):
                    retried += 1
                    if progress_callback:
                        progress_callback({
                            "state": "retrying_failed",
                            "current": index,
                            "total": total,
                            "account_id": account_id,
                            "account_name": account_name,
                            "message": f"Playlist retry already running {index}/{total}: {account_name}",
                            "wait_retry_count": retried,
                            "wait_failed_accounts": total,
                        })
                    continue
                logger.warning("Retry request for failed M3U account %s failed: %s", account_id, exc)
        return retried

    def _wait_for_m3u_refresh_completion(
        self,
        refreshed_accounts: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        wait_config = self._get_m3u_refresh_wait_config()
        if not wait_config["enabled"]:
            return {"ok": True, "state": "disabled", "message": "Playlist refresh wait disabled"}

        target_account_ids = {
            self._account_id_key(account.get("id"))
            for account in refreshed_accounts
            if isinstance(account, dict) and self._account_id_key(account.get("id")) is not None
        }
        if not target_account_ids:
            target_account_ids = None

        try:
            udi_manager = get_udi_manager()
        except Exception as exc:
            return {
                "ok": False,
                "state": "error",
                "message": f"Could not initialize UDI refresh monitor: {exc}",
            }

        timeout_seconds = wait_config["timeout_seconds"]
        poll_interval = wait_config["poll_interval_seconds"]
        min_wait = wait_config["min_wait_seconds"]
        stable_required = wait_config["stable_polls_required"]
        started = time.time()
        deadline = started + timeout_seconds
        last_signature = None
        stable_polls = 0
        last_snapshot: Dict[str, Any] = {}
        retried_failed_accounts = False
        retry_accepted_count = 0

        while True:
            if self._is_manual_stop_requested():
                message = self._manual_stop_message()
                elapsed = int(time.time() - started)
                if progress_callback:
                    progress_callback({
                        "state": "aborted",
                        "current": 0,
                        "total": 1,
                        "message": message,
                        "wait_elapsed_seconds": elapsed,
                        "wait_stable_polls": stable_polls,
                        "wait_busy_accounts": last_snapshot.get("busy_count"),
                        "wait_streams_seen": last_snapshot.get("stream_count"),
                        "wait_failed_accounts": last_snapshot.get("failed_count"),
                        "wait_retry_count": retry_accepted_count,
                    })
                return {
                    "ok": False,
                    "state": "aborted",
                    "message": message,
                    "snapshot": last_snapshot,
                    "elapsed_seconds": elapsed,
                    "retry_accepted_count": retry_accepted_count,
                }

            snapshot = self._build_m3u_refresh_monitor_snapshot(udi_manager, target_account_ids)
            last_snapshot = snapshot
            elapsed = int(time.time() - started)

            signature = snapshot.get("signature")
            if signature and signature == last_signature:
                stable_polls += 1
            else:
                stable_polls = 1 if signature else 0
                last_signature = signature

            busy_count = int(snapshot.get("busy_count") or 0)
            failed_count = int(snapshot.get("failed_count") or 0)
            account_count = int(snapshot.get("account_count") or 0)
            failed_accounts = list(snapshot.get("failed_accounts") or [])
            stream_count = snapshot.get("stream_count")
            accounts_ok = bool(snapshot.get("accounts_ok"))
            streams_ok = bool(snapshot.get("streams_ok"))
            stable_enough = stable_polls >= stable_required and elapsed >= min_wait
            no_busy_accounts = busy_count == 0
            all_monitored_accounts_failed = bool(
                failed_count > 0
                and account_count > 0
                and failed_count >= account_count
                and no_busy_accounts
            )
            if busy_count > 0 and account_count > 0:
                progress_current = max(0, account_count - busy_count)
                progress_total = account_count
                progress_message = (
                    f"Waiting for playlist parsing to finish "
                    f"({progress_current}/{account_count} providers ready, {elapsed}s)"
                )
            else:
                progress_current = min(stable_polls, stable_required)
                progress_total = stable_required
                progress_message = (
                    f"Waiting for playlist refresh to settle "
                    f"({progress_current}/{stable_required} stable polls, {elapsed}s)"
                )

            if progress_callback:
                progress_callback({
                    "state": "waiting",
                    "current": progress_current,
                    "total": progress_total,
                    "message": progress_message,
                    "wait_elapsed_seconds": elapsed,
                    "wait_stable_polls": stable_polls,
                    "wait_busy_accounts": busy_count,
                    "wait_streams_seen": stream_count,
                    "wait_failed_accounts": failed_count,
                    "wait_retry_count": retry_accepted_count,
                })

            if (
                failed_count > 0
                and no_busy_accounts
                and wait_config.get("retry_failed_providers")
                and not retried_failed_accounts
                and failed_accounts
            ):
                retried_failed_accounts = True
                retry_accepted_count = self._retry_failed_m3u_refresh_accounts(
                    failed_accounts,
                    progress_callback=progress_callback,
                )
                stable_polls = 0
                last_signature = None
                sleep_for = min(poll_interval, max(0.0, deadline - time.time()))
                if sleep_for > 0:
                    self._manual_stop_requested.wait(timeout=sleep_for)
                continue

            if all_monitored_accounts_failed:
                return {
                    "ok": False,
                    "state": "failed",
                    "message": "All monitored playlist refreshes failed",
                    "snapshot": snapshot,
                    "elapsed_seconds": elapsed,
                    "retry_accepted_count": retry_accepted_count,
                }

            if no_busy_accounts and stable_enough and (accounts_ok or streams_ok or stream_count is not None):
                final_state = "partial" if failed_count > 0 else "settled"
                final_message = (
                    f"Playlist refresh settled with {failed_count} failed provider"
                    f"{'' if failed_count == 1 else 's'}"
                    if failed_count > 0
                    else "Playlist refresh settled"
                )
                if progress_callback:
                    progress_callback({
                        "state": final_state,
                        "current": stable_required,
                        "total": stable_required,
                        "message": final_message,
                        "wait_elapsed_seconds": elapsed,
                        "wait_stable_polls": stable_polls,
                        "wait_busy_accounts": busy_count,
                        "wait_streams_seen": stream_count,
                        "wait_failed_accounts": failed_count,
                        "wait_retry_count": retry_accepted_count,
                    })
                return {
                    "ok": True,
                    "state": final_state,
                    "message": final_message,
                    "snapshot": snapshot,
                    "elapsed_seconds": elapsed,
                    "retry_accepted_count": retry_accepted_count,
                }

            if time.time() >= deadline:
                if progress_callback:
                    progress_callback({
                        "state": "timeout",
                        "current": min(stable_polls, stable_required),
                        "total": stable_required,
                        "message": "Timed out waiting for playlist refresh to settle",
                        "wait_elapsed_seconds": elapsed,
                        "wait_stable_polls": stable_polls,
                        "wait_busy_accounts": busy_count,
                        "wait_streams_seen": stream_count,
                        "wait_failed_accounts": failed_count,
                        "wait_retry_count": retry_accepted_count,
                    })
                return {
                    "ok": False,
                    "state": "timeout",
                    "message": "Timed out waiting for playlist refresh to settle",
                    "snapshot": last_snapshot,
                    "elapsed_seconds": elapsed,
                    "retry_accepted_count": retry_accepted_count,
                }

            sleep_for = min(poll_interval, max(0.0, deadline - time.time()))
            if sleep_for > 0:
                self._manual_stop_requested.wait(timeout=sleep_for)

    def _finish_run_status(
        self,
        status: Optional[str] = None,
        message: str = "",
        *,
        state: Optional[str] = None,
        stage: Optional[str] = None,
        stage_label: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self._ensure_run_status_fields()
        now = datetime.now()
        final_state = state or status or "completed"
        final_stage = stage or final_state
        final_stage_label = stage_label or self._stage_label(final_stage)
        snapshot_to_store = None
        with self._run_status_lock:
            status_data = self._run_status
            active_stage = status_data.get("stage")
            status_data["state"] = final_state
            status_data["status"] = final_state
            status_data["active"] = False
            status_data["stage"] = final_stage
            status_data["stage_label"] = final_stage_label
            status_data["message"] = message
            status_data["updated_at"] = now.isoformat()
            status_data["completed_at"] = now.isoformat()
            if error is not None:
                status_data["last_error"] = error

            started_at = status_data.get("started_at")
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at)
                    duration = (now - started).total_seconds()
                    status_data["duration_seconds"] = round(duration, 3)
                    status_data["elapsed_seconds"] = int(duration)
                except (TypeError, ValueError):
                    pass
            stage_started_at = status_data.get("stage_started_at")
            if stage_started_at:
                try:
                    stage_started = datetime.fromisoformat(stage_started_at)
                    stage_duration = (now - stage_started).total_seconds()
                    status_data["stage_duration_seconds"] = round(stage_duration, 3)
                    status_data["stage_elapsed_seconds"] = int(stage_duration)
                except (TypeError, ValueError):
                    pass
            for stage_item in status_data.get("stages", []):
                if final_state in {"completed", "completed_degraded"} and stage_item["status"] == "pending":
                    stage_item["status"] = "skipped"
                if stage_item["key"] == active_stage and stage_item["status"] == "running":
                    stage_item["status"] = (
                        "completed"
                        if final_state in {"completed", "completed_degraded"}
                        else final_state
                    )
            snapshot_to_store = copy.deepcopy(status_data.get("run_snapshot"))
        self._store_run_snapshot(snapshot_to_store)

    def _queue_run_status(self, message: str) -> None:
        """Mark the current automation intent as queued, not completed."""
        self._ensure_run_status_fields()
        now = datetime.now()
        with self._run_status_lock:
            status_data = self._run_status
            active_stage = status_data.get("stage")
            status_data["state"] = "queued"
            status_data["status"] = "queued"
            status_data["active"] = False
            status_data["stage"] = "queued"
            status_data["stage_label"] = "Queued"
            status_data["message"] = message
            status_data["updated_at"] = now.isoformat()
            status_data["completed_at"] = None
            for stage_item in status_data.get("stages", []):
                if stage_item["key"] == active_stage and stage_item["status"] == "running":
                    stage_item["status"] = "queued"

    def _manual_stop_message(self) -> str:
        return "Automation run was stopped by the user"

    def _current_run_id(self) -> Optional[str]:
        self._ensure_run_status_fields()
        with self._run_status_lock:
            run_id = self._run_status.get("run_id")
        return str(run_id) if run_id else None

    def _persist_manual_stop_request(self) -> None:
        try:
            from apps.database.manager import get_db_manager

            get_db_manager().set_system_setting(
                self.MANUAL_STOP_REQUEST_KEY,
                {
                    "run_id": self._current_run_id(),
                    "all_active": True,
                    "requested_at": datetime.now().isoformat(),
                },
            )
        except Exception as exc:
            logger.debug(f"Could not persist automation stop request: {exc}")

    def _clear_persisted_manual_stop_request(self) -> None:
        try:
            from apps.database.manager import get_db_manager

            get_db_manager().set_system_setting(self.MANUAL_STOP_REQUEST_KEY, None)
        except Exception as exc:
            logger.debug(f"Could not clear automation stop request: {exc}")

    def _persisted_manual_stop_matches_current_run(self) -> bool:
        try:
            from apps.database.manager import get_db_manager

            request = get_db_manager().get_system_setting(self.MANUAL_STOP_REQUEST_KEY, None)
        except Exception as exc:
            logger.debug(f"Could not read automation stop request: {exc}")
            return False

        if not isinstance(request, dict):
            return False

        self._ensure_run_status_fields()
        with self._run_status_lock:
            current_run_id = self._run_status.get("run_id")
            active_run = bool(
                self._run_status.get("active") or self._run_status.get("state") == "running"
            )

        requested_run_id = request.get("run_id")
        if requested_run_id and current_run_id and str(requested_run_id) == str(current_run_id):
            return True
        return bool(request.get("all_active") and active_run)

    def _is_manual_stop_requested(self) -> bool:
        self._ensure_run_status_fields()
        if self._manual_stop_requested.is_set():
            return True
        if self._persisted_manual_stop_matches_current_run():
            self._manual_stop_requested.set()
            return True
        return False

    def _abort_run_if_manual_stop_requested(
        self,
        *,
        active_periods: Optional[Dict[Any, Dict[str, Any]]] = None,
    ) -> bool:
        if not self._is_manual_stop_requested():
            return False

        if active_periods:
            self._advance_period_run_timestamps(
                active_periods,
                "aborted",
                manual_stop=True,
            )
        message = self._manual_stop_message()
        self._finish_run_status(
            state="aborted",
            stage="aborted",
            stage_label="Aborted",
            message=message,
            error=message,
        )
        self._manual_stop_requested.clear()
        self._clear_persisted_manual_stop_request()
        return True

    def _finish_cycle_outcome(
        self,
        *,
        refresh_success: bool,
        cycle_abort_message: Optional[str],
        cycle_failed_message: Optional[str] = None,
        refresh_degraded: bool = False,
    ) -> str:
        if not cycle_abort_message and self._is_manual_stop_requested():
            cycle_abort_message = self._manual_stop_message()

        if refresh_success and not cycle_abort_message and not cycle_failed_message:
            if refresh_degraded:
                self._finish_run_status(
                    state="completed_degraded",
                    stage="completed_degraded",
                    stage_label="Completed with Warnings",
                    message="Automation cycle completed with provider refresh warnings",
                )
                return "completed_degraded"
            self._finish_run_status(
                state="completed",
                stage="completed",
                stage_label="Completed",
                message="Automation cycle completed",
            )
            return "completed"

        if cycle_abort_message:
            self._finish_run_status(
                state="aborted",
                stage="aborted",
                stage_label="Aborted",
                message=cycle_abort_message,
                error=cycle_abort_message,
            )
            return "aborted"

        failed_message = cycle_failed_message or "Automation cycle stopped before matching completed"
        self._finish_run_status(
            state="failed",
            stage="failed",
            stage_label="Failed",
            message=failed_message,
            error=failed_message,
        )
        return "failed"

    def _advance_period_run_timestamps(
        self,
        active_periods: Dict[Any, Dict[str, Any]],
        run_job_outcome: str,
        *,
        manual_stop: bool = False,
    ) -> bool:
        """Advance scheduler timers for completed runs and deliberate manual stops."""
        if not active_periods:
            return False

        should_advance = run_job_outcome in {"completed", "completed_degraded"} or (
            manual_stop and run_job_outcome == "aborted"
        )
        if not should_advance:
            logger.info(
                "Not advancing automation period timers after %s run; next scheduler pass may retry",
                run_job_outcome,
            )
            return False

        now = datetime.now()
        for p_id_tuple in active_periods.keys():
            # active_periods keys are (p_id, p_name)
            pid = p_id_tuple[0]
            self.period_last_run[pid] = now

        # Keep legacy last_playlist_update synced for legacy backward compatibility if any
        self.last_playlist_update = now
        self._save_state()
        return True

    @staticmethod
    def _summarize_quality_check_results(check_results, expected_count: int) -> Dict[str, Any]:
        checked_count = len(check_results or {})
        expected_count = max(0, int(expected_count or 0))
        aborted_count = 0
        failed_count = 0
        first_abort_message = None

        for result in (check_results or {}).values():
            if not isinstance(result, dict):
                continue
            if result.get("aborted") or result.get("error") == "connectivity_guard":
                aborted_count += 1
                if first_abort_message is None:
                    first_abort_message = result.get("message") or "Quality check was aborted"
            elif result.get("success") is False or result.get("error"):
                failed_count += 1

        incomplete_count = max(0, expected_count - checked_count)
        return {
            "ok": aborted_count == 0 and incomplete_count == 0,
            "checked_count": checked_count,
            "expected_count": expected_count,
            "aborted_count": aborted_count,
            "failed_count": failed_count,
            "incomplete_count": incomplete_count,
            "abort_message": first_abort_message,
        }

    def get_run_status(self) -> Dict[str, Any]:
        """Return the current or most recent automation-cycle status."""
        self._ensure_run_status_fields()
        with self._run_status_lock:
            status = copy.deepcopy(self._run_status)

        if status.get("state") == "running":
            now = datetime.now()
            started_at = status.get("started_at")
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at)
                    status["duration_seconds"] = round((now - started).total_seconds(), 3)
                except (TypeError, ValueError):
                    pass
            stage_started_at = status.get("stage_started_at")
            if stage_started_at:
                try:
                    stage_started = datetime.fromisoformat(stage_started_at)
                    status["stage_duration_seconds"] = round((now - stage_started).total_seconds(), 3)
                except (TypeError, ValueError):
                    pass
            status["updated_at"] = now.isoformat()

        return status

    def get_status(self) -> Dict[str, Any]:
        """Legacy status snapshot used by older API/tests."""
        interval_minutes = self.config.get("playlist_update_interval_minutes", 5)
        next_playlist_update = None
        if self.automation_running:
            base_time = self.last_playlist_update or self.automation_start_time or datetime.now()
            next_playlist_update = (base_time + timedelta(minutes=interval_minutes)).isoformat()

        return {
            "running": self.automation_running,
            "automation_running": self.automation_running,
            "last_playlist_update": self.last_playlist_update.isoformat() if self.last_playlist_update else None,
            "next_playlist_update": next_playlist_update,
            "automation_start_time": self.automation_start_time.isoformat() if self.automation_start_time else None,
            "config": copy.deepcopy(self.config),
            "run_status": self.get_run_status(),
        }
    
    def _is_dead_stream_removal_enabled(self) -> bool:
        """Check if dead stream removal is enabled in stream checker config.

        Reads directly from the database on every call. The cache that previously
        guarded this read has been removed — the method is called at most once per
        automation cycle (not in a tight loop), so the DB round-trip is negligible.
        Removing the 60-second TTL cache eliminates a stale-value window during
        which a user's toggle change was silently ignored.

        Returns:
            True if dead stream removal is enabled, False otherwise
        """
        try:
            from apps.database.manager import get_db_manager
            config = get_db_manager().get_system_setting('stream_checker_config', {})
            if config:
                return config.get('dead_stream_handling', {}).get('enabled', True)
            legacy_config_file = Path(os.environ.get('CONFIG_DIR', str(CONFIG_DIR))) / 'stream_checker_config.json'
            if legacy_config_file.exists():
                with open(legacy_config_file, 'r', encoding='utf-8') as f:
                    legacy_config = json.load(f)
                return legacy_config.get('dead_stream_handling', {}).get('enabled', True)
            return True
        except Exception as e:
            logger.error(f"Error reading stream checker config from DB: {e}")
            return True

    def _get_channel_visibility_config(self, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            from apps.database.manager import get_db_manager

            config = get_db_manager().get_system_setting('stream_checker_config', {}) or {}
            global_config = config.get('channel_visibility_automation', {}) if isinstance(config, dict) else {}
            return resolve_channel_visibility_config(global_config, profile)
        except Exception as e:
            logger.error(f"Error reading channel visibility automation config from DB: {e}")
            return {}

    @staticmethod
    def _visibility_changelog_result(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not result:
            return None
        if result.get('action') in {'disabled', 'no_visibility_change', 'visible_unmanaged'}:
            return None
        return result

    def _record_channel_visibility_events(
        self,
        events: List[Dict[str, Any]],
        *,
        skip_changelog: bool,
        source: str,
    ) -> None:
        significant = [
            event for event in (self._visibility_changelog_result(item) for item in events)
            if event
        ]
        if not significant:
            return
        if skip_changelog or not self.config.get("enabled_features", {}).get("changelog_tracking", True):
            return
        try:
            self.changelog.add_entry("channel_visibility", {
                "source": source,
                "events": significant,
                "changed_count": sum(1 for event in significant if event.get("changed")),
                "total_events": len(significant),
            })
        except Exception as exc:
            logger.warning(f"Failed to write channel visibility changelog: {exc}")
    
    def _filter_channels_by_profile(self, all_channels: List[Dict], action_description: str) -> List[Dict]:
        """Filter channels by selected profile if one is configured.
        
        Args:
            all_channels: List of all channels from UDI
            action_description: Description of the action (e.g., "stream assignment", "stream validation")
                               Used in log messages to provide context
        
        Returns:
            Filtered list of channels. If no profile is selected or an error occurs,
            returns the original list.
        """
        # Legacy profile filtering is deprecated in favor of granular per-channel automation profiles.
        # This method is kept for backward compatibility but now returns all channels,
        # letting the specific logic (assignment/validation) handle per-channel profiles.
        return all_channels
    
    def refresh_playlists(
        self,
        force: bool = False,
        account_id: Optional[int] = None,
        skip_changelog: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[bool, List[Dict]]:
        """Refresh M3U playlists and track changes.
        
        Args:
            force: If True, bypass the auto_playlist_update feature flag check.
                   Used for manual/quick action triggers from the UI.
            account_id: Optional ID of specific account to refresh. If None, refreshes all enabled accounts.
            
        Returns:
            Tuple of (success_bool, refreshed_accounts_list)
        """
        refreshed_accounts = []
        refresh_failed = False
        failed_refresh_requests = []

        def emit_refresh_progress(payload: Dict[str, Any]) -> None:
            if not progress_callback:
                return
            try:
                progress_callback(payload)
            except Exception as callback_error:
                logger.debug(f"M3U refresh progress callback failed: {callback_error}")

        try:
            if not force and not self.config.get("enabled_features", {}).get("auto_playlist_update", True):
                if not force:  # Allow force to override feature flag
                    logger.info("Playlist update is disabled in configuration")
                    return RefreshResult(False, [], outcome="skipped")
            
            logger.info("Starting M3U playlist refresh...")
            
            # Get all M3U accounts
            all_accounts = get_m3u_accounts()
            self._m3u_accounts_cache = all_accounts
            logger.debug(f"M3U accounts fetched from UDI cache: {len(all_accounts) if all_accounts else 0} accounts")
            
            if all_accounts:
                # Filter out "custom" account and non-active accounts
                non_custom_accounts = [
                    acc for acc in all_accounts
                    if acc.get('name', '').lower() != 'custom' and acc.get('is_active', True)
                ]
                
                # Determine which accounts to refresh
                accounts_to_process = []
                
                if account_id is not None:
                    # Refresh specific account
                    target_account = next((a for a in non_custom_accounts if a.get('id') == account_id), None)
                    if target_account:
                        accounts_to_process = [target_account]
                    else:
                        logger.warning(f"Requested refresh for account {account_id}, but it was not found or is inactive/custom.")
                else:
                    # Refresh all (or filtered by enabled_m3u_accounts config)
                    enabled_accounts = self.config.get("enabled_m3u_accounts", [])
                    if enabled_accounts:
                         # Filter to only enabled accounts
                         accounts_to_process = [a for a in non_custom_accounts if a.get('id') in enabled_accounts]
                    else:
                         # All non-custom active accounts
                         accounts_to_process = non_custom_accounts

                # Execute refresh
                total_accounts = len(accounts_to_process)
                if total_accounts:
                    emit_refresh_progress({
                        "state": "planned",
                        "current": 0,
                        "total": total_accounts,
                        "message": f"Preparing {total_accounts} playlist refresh request(s)",
                    })

                for index, account in enumerate(accounts_to_process, start=1):
                    acc_id = account.get('id')
                    if acc_id is not None:
                        account_name = account.get('name', f"Account {acc_id}")
                        if self._account_is_refresh_busy(account):
                            logger.info(
                                "M3U account %s (%s) is already refreshing; monitoring existing refresh",
                                acc_id,
                                account_name,
                            )
                            refreshed_accounts.append({
                                "id": acc_id,
                                "name": account_name,
                                "already_running": True,
                            })
                            emit_refresh_progress({
                                "state": "already_running",
                                "current": index,
                                "total": total_accounts,
                                "account_id": acc_id,
                                "account_name": account_name,
                                "message": f"Playlist {index}/{total_accounts} already refreshing: {account_name}",
                            })
                            continue

                        logger.info(f"Refreshing M3U account {acc_id}: {account_name}")
                        emit_refresh_progress({
                            "state": "requesting",
                            "current": index - 1,
                            "total": total_accounts,
                            "account_id": acc_id,
                            "account_name": account_name,
                            "message": f"Refreshing playlist {index}/{total_accounts}: {account_name}",
                        })
                        try:
                            response = refresh_m3u_playlists(account_id=acc_id)
                        except Exception as exc:
                            if self._is_m3u_refresh_already_running_error(exc):
                                logger.info(
                                    "M3U account %s (%s) refresh is already running; monitoring existing refresh",
                                    acc_id,
                                    account_name,
                                )
                                refreshed_accounts.append({
                                    "id": acc_id,
                                    "name": account_name,
                                    "already_running": True,
                                })
                                emit_refresh_progress({
                                    "state": "already_running",
                                    "current": index,
                                    "total": total_accounts,
                                    "account_id": acc_id,
                                    "account_name": account_name,
                                    "message": f"Playlist {index}/{total_accounts} already refreshing: {account_name}",
                                })
                                continue

                            refresh_failed = True
                            failed_refresh_requests.append({
                                "id": acc_id,
                                "name": account_name,
                                "error": str(exc),
                            })
                            logger.error(
                                f"M3U refresh failed for account {acc_id} ({account_name}): {exc}"
                            )
                            emit_refresh_progress({
                                "state": "failed",
                                "current": index,
                                "total": total_accounts,
                                "account_id": acc_id,
                                "account_name": account_name,
                                "message": f"Playlist {index}/{total_accounts} failed: {account_name}",
                            })
                            continue

                        if self._is_m3u_refresh_response_success(response):
                            refreshed_accounts.append({
                                "id": acc_id,
                                "name": account_name,
                            })
                            emit_refresh_progress({
                                "state": "accepted",
                                "current": index,
                                "total": total_accounts,
                                "account_id": acc_id,
                                "account_name": account_name,
                                "message": f"Playlist {index}/{total_accounts} refresh accepted: {account_name}",
                            })
                        else:
                            refresh_failed = True
                            failed_refresh_requests.append({
                                "id": acc_id,
                                "name": account_name,
                                "status": getattr(response, 'status_code', None),
                            })
                            status = getattr(response, 'status_code', None)
                            logger.error(
                                f"M3U refresh failed for account {acc_id} ({account_name}), "
                                f"status={status}"
                            )
                            emit_refresh_progress({
                                "state": "failed",
                                "current": index,
                                "total": total_accounts,
                                "account_id": acc_id,
                                "account_name": account_name,
                                "message": f"Playlist {index}/{total_accounts} failed: {account_name}",
                            })
                
                if not accounts_to_process:
                    logger.info("No accounts matched criteria for refresh.")
                    emit_refresh_progress({
                        "state": "skipped",
                        "current": 0,
                        "total": 0,
                        "message": "No active playlists matched the refresh request",
                    })
            else:
                # Fallback: if we can't get accounts, refresh all (legacy behavior)
                logger.warning("Could not fetch M3U accounts, refreshing all as fallback")
                emit_refresh_progress({
                    "state": "requesting",
                    "current": 0,
                    "total": 1,
                    "message": "Refreshing all playlists through Dispatcharr",
                })
                try:
                    response = refresh_m3u_playlists()
                except Exception as exc:
                    if self._is_m3u_refresh_already_running_error(exc):
                        logger.info("Fallback M3U refresh is already running; monitoring existing refresh")
                        refreshed_accounts.append({
                            "id": None,
                            "name": "All playlists",
                            "already_running": True,
                        })
                        emit_refresh_progress({
                            "state": "already_running",
                            "current": 1,
                            "total": 1,
                            "message": "Fallback playlist refresh already running",
                        })
                        response = None
                    else:
                        refresh_failed = True
                        failed_refresh_requests.append({"id": None, "name": "All playlists", "error": str(exc)})
                        logger.error(f"Fallback M3U refresh failed: {exc}")
                        emit_refresh_progress({
                            "state": "failed",
                            "current": 1,
                            "total": 1,
                            "message": "Fallback playlist refresh failed",
                        })
                        response = None
                if response is not None and not self._is_m3u_refresh_response_success(response):
                    refresh_failed = True
                    failed_refresh_requests.append({"id": None, "name": "All playlists"})
                    status = getattr(response, 'status_code', None)
                    logger.error(f"Fallback M3U refresh failed, status={status}")
                    emit_refresh_progress({
                        "state": "failed",
                        "current": 1,
                        "total": 1,
                        "message": "Fallback playlist refresh failed",
                    })
                else:
                    emit_refresh_progress({
                        "state": "accepted",
                        "current": 1,
                        "total": 1,
                        "message": "Fallback playlist refresh request accepted",
                    })
                    refreshed_accounts.append({
                        "id": None,
                        "name": "All playlists",
                    })

            if refresh_failed:
                failed_count = len(failed_refresh_requests)
                if refreshed_accounts:
                    logger.warning(
                        "Playlist refresh accepted %s request(s) with %s provider request failure(s)",
                        len(refreshed_accounts),
                        failed_count,
                    )
                    emit_refresh_progress({
                        "state": "partial",
                        "current": len(refreshed_accounts),
                        "total": (len(refreshed_accounts) + failed_count),
                        "message": (
                            f"Playlist refresh partially accepted: {len(refreshed_accounts)} accepted, "
                            f"{failed_count} failed"
                        ),
                        "failed_refresh_requests": failed_count,
                    })
                else:
                    logger.error("Playlist refresh encountered failed account refreshes and none were accepted")
                    return RefreshResult(
                        False,
                        refreshed_accounts,
                        failed_refresh_requests=failed_refresh_requests,
                    )
            
            # NOTE: UDI refresh, changelog write, and dead stream cleanup are
            # intentionally NOT performed here. run_automation_cycle() owns all
            # three after refresh_playlists() returns, using a UDI sync to ensure
            # accurate data. Doing them here would use a stale pre-fetch cache.

            # Trigger EPG matching to pick up any EPG/tvg-id changes made in Dispatcharr
            # This ensures that if a channel's EPG assignment was changed in Dispatcharr,
            # the new program data will be available in StreamFlow
            try:
                logger.info("Triggering auto-create rule matching after playlist update...")
                from apps.automation.scheduling_service import get_scheduling_service
                scheduling_service = get_scheduling_service()
                # Force matching to bypass cache and get fresh EPG data
                scheduling_service.match_programs_to_rules()
                logger.info("Rule matching completed successfully")
            except Exception as e:
                logger.error(f"Error triggering rule matching after playlist update: {e}")
                # Continue even if EPG refresh fails

            self.last_playlist_update = datetime.now()
            logger.info("M3U playlist refresh requests accepted successfully")

            # Note: Channel marking for stream quality checking is handled in discover_and_assign_streams()
            # after streams are actually assigned to specific channels. This prevents marking all channels
            # when we only know that *some* streams changed in the playlist, not which channels are affected.

            return RefreshResult(
                True,
                refreshed_accounts,
                failed_refresh_requests=failed_refresh_requests,
            )
            
        except Exception as e:
            logger.error(f"Failed to refresh M3U playlists: {e}")
            
            
            if not skip_changelog and self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_entry("playlist_refresh", {
                    "success": False,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                })
            
            return RefreshResult(False, [], outcome="failed")

    def _is_m3u_refresh_response_success(self, response: Any) -> bool:
        """Validate M3U refresh API responses.

        Accepts mock responses used in tests. If no status code is present,
        assume success to preserve compatibility with simplified mocks.
        """
        if response is None:
            return False

        status_code = getattr(response, 'status_code', None)
        if status_code is None:
            return True

        try:
            code = int(status_code)
        except (TypeError, ValueError):
            return False

        return 200 <= code < 300

    @staticmethod
    def _is_m3u_refresh_already_running_error(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = None
        if code in {409, 423}:
            return True

        text = " ".join(
            str(value or "")
            for value in (
                exc,
                getattr(response, "text", ""),
                getattr(response, "reason", ""),
            )
        ).casefold()
        return any(marker in text for marker in ("already running", "already refreshing", "refresh in progress"))

    def _should_abort_for_suspicious_stream_pool(self, before_count: int, after_count: int, playlists_refreshed: bool) -> bool:
        """Return True when the post-refresh stream pool looks unsafe.

        Safety checks apply only when playlists were refreshed in this cycle.
        """
        if not playlists_refreshed:
            return False

        if before_count <= 0:
            return False

        if after_count <= 0:
            logger.error(
                "Aborting matching: stream pool is empty after playlist refresh "
                f"(before={before_count}, after={after_count})"
            )
            return True

        safety_cfg = self.config.get('automation_safety', {}) if isinstance(self.config, dict) else {}
        min_ratio = safety_cfg.get('min_stream_pool_ratio_after_refresh', 0.5)
        min_drop = safety_cfg.get('min_stream_pool_drop_after_refresh', 100)

        try:
            min_ratio = max(0.0, min(1.0, float(min_ratio)))
        except (TypeError, ValueError):
            min_ratio = 0.5

        try:
            min_drop = max(1, int(min_drop))
        except (TypeError, ValueError):
            min_drop = 100

        drop = before_count - after_count
        ratio = after_count / before_count

        if drop >= min_drop and ratio < min_ratio:
            logger.error(
                "Aborting matching: suspicious stream pool drop after playlist refresh "
                f"(before={before_count}, after={after_count}, drop={drop}, ratio={ratio:.3f}, "
                f"threshold_ratio={min_ratio}, threshold_drop={min_drop})"
            )
            return True

        return False
    def _match_streams_batch(self, streams: List[Dict], channel_streams: Dict[str, set],
                             dead_stream_removal_enabled: bool,
                             channel_to_revive_enabled: Dict[str, bool] = None,
                             channel_tvg_map: Dict[str, str] = None,
                             channel_to_match_priorities: Dict[str, List[str]] = None,
                             channel_to_group_map: Dict[str, Any] = None,
                             channel_name_map: Dict[str, str] = None) -> Tuple[Dict[str, List[str]], Dict[str, List[Dict]]]:
        """
        Process a batch of streams for regex matching.
        This method is designed to be run in a separate thread.
        
        Args:
            streams: List of stream dictionaries to process
            channel_streams: Dict of existing channel streams {channel_id: {stream_ids}}
            dead_stream_removal_enabled: Whether to skip dead streams
            channel_to_revive_enabled: Mapping of channel IDs to their Stream Revival setting
            channel_tvg_map: Mapping of channel IDs to their TVG-ID (optional)
            channel_to_match_priorities: Mapping of channel IDs to priority order (optional)
            
        Returns:
            Tuple of (assignments, assignment_details)
        """
        assignments = defaultdict(list)
        assignment_details = defaultdict(list)
        channel_to_revive_enabled = channel_to_revive_enabled or {}
        channel_tvg_map = channel_tvg_map or {}
        channel_to_match_priorities = channel_to_match_priorities or {}
        channel_to_group_map = channel_to_group_map or {}
        channel_name_map = channel_name_map or {}
        match_stream_to_channels = self.regex_matcher.match_stream_to_channels

        # Cache match outcomes for repeated stream signatures inside this batch.
        stream_match_cache: Dict[Tuple[str, Any, Optional[str]], Tuple[str, ...]] = {}
        
        for stream in streams:
            # Validate that stream is a dictionary before accessing attributes
            if not isinstance(stream, dict):
                continue
                
            stream_name = stream.get('name', '')
            stream_id = stream.get('id')
            
            if not stream_name or not stream_id:
                continue
            
            # Get stream's url and m3u_account
            stream_url = stream.get('url', '')
            stream_m3u_account = stream.get('m3u_account')
            stream_tvg_id = stream.get('tvg_id')
            match_cache_key = (stream_name, stream_m3u_account, stream_tvg_id)
            
            matching_channels = stream_match_cache.get(match_cache_key)
            if matching_channels is None:
                matching_channels = tuple(match_stream_to_channels(
                    stream_name,
                    stream_m3u_account,
                    stream_tvg_id,
                    channel_tvg_map,
                    channel_to_match_priorities,
                    channel_to_group_map,
                    channel_name_map,
                ))
                stream_match_cache[match_cache_key] = matching_channels

            if not matching_channels:
                continue

            # Check if any matching channel allows reviving dead streams
            any_revive_enabled = False
            for ch_id in matching_channels:
                if channel_to_revive_enabled.get(str(ch_id), False):
                    any_revive_enabled = True
                    break

            # Skip dead streams if removal is enabled globally
            if self.dead_streams_tracker and self.dead_streams_tracker.is_dead(stream_url):
                if dead_stream_removal_enabled:
                    # If any matched channel has Stream Revival enabled, we DON'T skip it, 
                    # even if it's offline. We want it added so the checker can re-evaluate it.
                    if any_revive_enabled:
                        # logger.debug(f"Allowing dead stream {stream_id} for potential revival")
                        pass
                    else:
                        # Revival is disabled for all matching channels - skip ALL dead streams
                        # This prevents the continuous re-addition loop for low quality/failed streams
                        # logger.debug(f"Skipping dead stream {stream_id} (revival disabled)")
                        continue
            
            for channel_id in matching_channels:
                # Check if stream is already in this channel
                if channel_id in channel_streams and stream_id not in channel_streams[channel_id]:
                    assignments[channel_id].append(stream_id)
                    assignment_details[channel_id].append({
                        "stream_id": stream_id,
                        "stream_name": stream_name
                    })
                    
        return assignments, assignment_details

    def _validate_channels_batch(self, channels: List[Dict], stream_lookup: Dict[int, Dict], 
                               matching_enabled_channel_ids: List[str],
                               channel_validation_settings: Dict[str, Dict] = None,
                               full_channel_tvg_map: Dict[str, str] = None) -> Dict[str, Any]:
        """
        Process a batch of channels for stream validation.
        This method is designed to be run in a separate thread.
        
        Args:
            channels: List of channel dictionaries to process
            stream_lookup: Dict of all streams {stream_id: stream_data}
            matching_enabled_channel_ids: List of channel IDs where matching is enabled
            channel_validation_settings: Dict of channel ID -> validation settings
            full_channel_tvg_map: Dict mapping channel_id -> tvg_id for ALL channels (not just the batch)
            
        Returns:
            Dict containing partial validation results
        """
        results = {
            "channels_checked": 0,
            "streams_removed": 0,
            "channels_modified": 0,
            "details": []
        }
        
        udi = get_udi_manager() # Singleton, thread-safe access
        
        if channel_validation_settings is None:
            channel_validation_settings = {}
        
        
        # Use the full channel TVG-ID map passed in from the impl, which covers ALL channels.
        # A batch-scoped map causes false negatives for cross-batch TVG lookups (Bug 2 fix).
        channel_tvg_map = full_channel_tvg_map if full_channel_tvg_map is not None else {}
        channel_to_group_map = {}
        channel_name_map = {}
        for ch in channels:
            if not isinstance(ch, dict) or ch.get('id') is None:
                continue
            cid = str(ch.get('id'))
            channel_name_map[cid] = ch.get('name', '')
            gid = ch.get('group_id') if ch.get('group_id') is not None else ch.get('channel_group_id')
            if gid is not None:
                channel_to_group_map[cid] = gid

        if not channel_tvg_map:
            # Defensive fallback: build from the batch if no global map was provided
            for ch in channels:
                if isinstance(ch, dict) and ch.get('tvg_id'):
                    channel_tvg_map[str(ch.get('id'))] = ch.get('tvg_id')
                
        for channel in channels:
            channel_id = channel.get('id')
            channel_name = channel.get('name', f'Channel {channel_id}')
            
            # Skip channels with matching disabled
            if channel_id not in matching_enabled_channel_ids:
                continue
            
            # Skip channels without matching criteria (no regex AND no TVG-ID matching)
            group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
            if not self.regex_matcher.has_regex_patterns(str(channel_id), group_id) and not self.regex_matcher.get_match_by_tvg_id(str(channel_id), group_id):
                continue
            
            results["channels_checked"] += 1
            
            # Get settings for this channel
            settings = channel_validation_settings.get(channel_id, {})
            validate_enabled = settings.get("validate_enabled", False)
            
            # Get streams for this channel
            channel_streams = udi.get_channel_streams(channel_id)
            if not channel_streams:
                continue
            
            streams_to_keep = []
            streams_to_remove = []
            
            for stream in channel_streams:
                if not isinstance(stream, dict) or 'id' not in stream:
                    continue
                
                stream_id = stream['id']
                stream_name_in_channel = stream.get('name', '')
                
                # Look up full stream data to get m3u_account and up-to-date name
                full_stream = stream_lookup.get(stream_id)
                if not full_stream:
                    # Stream not found in UDI, keep it safe
                    streams_to_keep.append(stream_id)
                    continue
                
                stream_name = full_stream.get('name', stream_name_in_channel)
                stream_m3u_account = full_stream.get('m3u_account')
                
                # Check Regex/TVG-ID Validity
                # Check if stream still matches this channel
                stream_tvg_id = full_stream.get('tvg_id')
                matching_channels = self.regex_matcher.match_stream_to_channels(
                    stream_name,
                    stream_m3u_account,
                    stream_tvg_id,
                    channel_tvg_map,
                    None,
                    channel_to_group_map,
                    channel_name_map,
                )
                
                if str(channel_id) in matching_channels:
                    streams_to_keep.append(stream_id)
                else:
                    streams_to_remove.append({
                        "id": stream_id, 
                        "name": stream_name,
                        "reason": "regex_mismatch"
                    })
            
            if streams_to_remove:
                results["streams_removed"] += len(streams_to_remove)
                results["channels_modified"] += 1
                results["details"].append({
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "removed_count": len(streams_to_remove),
                    "removed_streams": streams_to_remove,
                    "kept_ids": streams_to_keep,
                    "validate_enabled": validate_enabled
                })
                
        return results

    def discover_and_assign_streams(self, force: bool = False, skip_check_trigger: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None, allow_dead_streams: Optional[bool] = None) -> Dict[str, int]:
        """Wrapper for stream discovery to ensure single execution.

        Args:
            allow_dead_streams: When provided, overrides the global dead_stream_handling
                config for this call. Used by check_single_channel to pass the
                profile-resolved flag so the matching step respects the same policy
                as the checking step (Bug 3 fix). When None (default), falls back to
                _is_dead_stream_removal_enabled() so the global automation path is
                unaffected.
        """
        if not self._lock.acquire(blocking=False):
            logger.warning("Stream discovery already active - skipping concurrent request")
            return {}
        
        try:
            return self._discover_and_assign_streams_impl(force, skip_check_trigger, forced_period_id, skip_changelog, channel_id, allow_dead_streams=allow_dead_streams)
        finally:
            self._lock.release()

    def _mark_checking_only_channels(self, checking_only_channel_ids: List[int], udi, skip_check_trigger: bool) -> None:
        """Mark matching-disabled/checking-enabled channels for quality checks."""
        if not checking_only_channel_ids:
            return

        try:
            checker = get_stream_checker_service()
            stream_counts = {}
            for channel_id in checking_only_channel_ids:
                try:
                    channel = udi.get_channel_by_id(channel_id)
                    if channel:
                        streams = channel.get('streams', [])
                        stream_counts[channel_id] = len(streams) if isinstance(streams, list) else 0
                except Exception:
                    pass

            checker.update_tracker.mark_channels_updated(
                checking_only_channel_ids,
                stream_counts=stream_counts,
            )
            logger.info(
                f"Marked {len(checking_only_channel_ids)} checking-only "
                "channel(s) for quality checking"
            )
            if not skip_check_trigger:
                checker.trigger_check_updated_channels()
            else:
                logger.debug(
                    "Skipping check trigger for checking-only channels "
                    "(will be handled by caller)"
                )
        except Exception as exc:
            logger.debug(f"Could not mark checking-only channels for quality checking: {exc}")

    def _discover_and_assign_streams_impl(self, force: bool = False, skip_check_trigger: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None, allow_dead_streams: Optional[bool] = None) -> Dict[str, int]:
        """Discover new streams and assign them to channels based on regex patterns.
        
        Args:
            force: If True, bypass the auto_stream_discovery feature flag check.
                   Used for manual/quick action triggers from the UI.
            skip_check_trigger: If True, don't trigger immediate stream quality check.
                   Used when the caller will handle the check itself (e.g., check_single_channel).
            forced_period_id: Optional period ID to filter channels.
            channel_id: Optional channel ID to scope discovery to a single channel.
                        When provided, only that channel receives stream assignments and
                        all_streams is pre-filtered to globally-enabled M3U accounts
                        (same boundary as full discovery) so that streams from new or
                        previously-unassigned providers are still considered.
                        All other callers pass None (default) for full discovery.
            allow_dead_streams: When provided by the caller (e.g. check_single_channel),
                        overrides the global dead_stream_handling config for this run.
                        When None, falls back to _is_dead_stream_removal_enabled().
        """
        if not force and not self.config.get("enabled_features", {}).get("auto_stream_discovery", True):
            logger.info("Stream discovery is disabled in configuration")
            return {}
        
        try:
            # Reload patterns to ensure we have the latest changes
            self.regex_matcher.reload_patterns()
            
            logger.info("Starting stream discovery and assignment...")
            
            # Get all available streams (don't log, we already logged during refresh)
            all_streams = get_streams(log_result=False)
            if not all_streams:
                logger.warning("No streams found")
                all_streams = []
            
            # Validate that all_streams is a list
            if not isinstance(all_streams, list):
                logger.error(f"Invalid streams response format: expected list, got {type(all_streams).__name__}")
                return {}
            
            # Filter streams by enabled M3U accounts
            # Use cached M3U accounts if available (from refresh_playlists), otherwise fetch
            # This optimization ensures M3U accounts are only queried once per playlist refresh cycle
            if self._m3u_accounts_cache is not None:
                all_accounts = self._m3u_accounts_cache
                logger.debug(f"Using cached M3U accounts from playlist refresh (no UDI/API call - {len(all_accounts) if all_accounts else 0} accounts)")
            else:
                all_accounts = get_m3u_accounts()
                logger.debug(f"Fetched M3U accounts from UDI cache (cache was empty - {len(all_accounts) if all_accounts else 0} accounts)")
            enabled_account_ids = set()
            
            if all_accounts:
                # Filter specific accounts
                non_custom_accounts = [
                    acc for acc in all_accounts
                    if acc.get('is_active', True)
                ]
                
                # Get enabled accounts from config
                enabled_accounts_config = self.config.get("enabled_m3u_accounts", [])
                
                if enabled_accounts_config:
                    # Only include accounts that are in the enabled list
                    enabled_account_ids = set(
                        acc.get('id') for acc in non_custom_accounts 
                        if acc.get('id') in enabled_accounts_config and acc.get('id') is not None
                    )
                else:
                    # If no specific accounts are enabled in config, use all non-custom active accounts
                    enabled_account_ids = set(
                        acc.get('id') for acc in non_custom_accounts 
                        if acc.get('id') is not None
                    )
                
                # Filter streams to only include those from enabled accounts
                # Also include custom streams (is_custom=True) as they don't belong to an M3U account
                filtered_streams = [
                    stream for stream in all_streams
                    if stream.get('is_custom', False) or stream.get('m3u_account') in enabled_account_ids
                ]
                
                streams_filtered_count = len(all_streams) - len(filtered_streams)
                if streams_filtered_count > 0:
                    logger.info(f"Filtered out {streams_filtered_count} streams from disabled/inactive M3U accounts")
                
                all_streams = filtered_streams
                
                if not all_streams:
                    logger.info("No streams found after filtering by enabled M3U accounts")
                    return {}
            else:
                logger.warning("Could not fetch M3U accounts, using all streams")
            
            # Get all channels from UDI
            udi = get_udi_manager()
            all_channels = get_channels()
            if not all_channels:
                logger.warning("No channels found")
                return {}
            
            # Filter by profile if one is selected
            all_channels = self._filter_channels_by_profile(all_channels, "stream assignment")

            # Scope to a single channel when called from check_single_channel.
            # This avoids iterating and assigning streams for every channel in
            # the system when only one channel needs updating.
            if channel_id is not None:
                all_channels = [ch for ch in all_channels if ch.get('id') == channel_id]
                if not all_channels:
                    logger.info(f"[single-channel] Channel {channel_id} not found or not eligible for stream assignment")
                    return {}
                logger.info(f"[single-channel] Scoping stream discovery to channel {channel_id}")

                # Pre-filter all_streams to only streams from globally-enabled M3U accounts.
                # This mirrors what the full-discovery path already applied above, so the
                # stream catalogue here is identical to a normal full-discovery run —
                # it just skips iterating/assigning every other channel.
                #
                # Previously this block narrowed the catalogue to accounts the channel
                # *already* had assigned streams from, which prevented newly added M3U
                # providers (or existing providers that added the event later) from ever
                # being matched to this channel during a single-channel check.
                if enabled_account_ids:
                    pre_filter_count = len(all_streams)
                    all_streams = [
                        s for s in all_streams
                        if s.get('is_custom', False)
                        or (s.get('m3u_account_id') or s.get('m3u_account')) in enabled_account_ids
                    ]
                    logger.info(
                        f"[single-channel] Pre-filtered streams from {pre_filter_count} "
                        f"to {len(all_streams)} (enabled accounts: {enabled_account_ids})"
                    )

            # Filter channels by automation profile settings
            automation_config = get_automation_config_manager()
            matching_enabled_channel_ids = []
            channel_to_revive_enabled = {}
            channel_tvg_map = {}
            channel_to_match_priorities = {}
            channel_to_group_map = {}
            channel_name_map = {}
            channel_visibility_events = []
            
            
            for channel in all_channels:
                if not isinstance(channel, dict) or 'id' not in channel:
                    continue
                channel_id = channel['id']
                channel_name_map[str(channel_id)] = channel.get('name', f'Channel {channel_id}')
                channel_tvg_id = channel.get('tvg_id')
                if channel_tvg_id:
                    channel_tvg_map[str(channel_id)] = channel_tvg_id
                group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                if group_id is not None:
                    channel_to_group_map[str(channel_id)] = group_id
                
                # Resolve group key from either UDI shape to include group-only period assignments.
                effective_group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                # Get effective configuration - only channels with automation periods participate
                config = automation_config.get_effective_configuration(channel_id, effective_group_id)
                
                # Skip channels without automation periods assigned
                if not config:
                    legacy_match_config = self.regex_matcher._get_effective_channel_config(channel_id, effective_group_id)
                    has_legacy_match_config = bool(
                        legacy_match_config
                        and legacy_match_config.get("enabled", True)
                        and (
                            legacy_match_config.get("match_by_tvg_id", False)
                            or legacy_match_config.get("regex_patterns")
                        )
                    )
                    if has_legacy_match_config:
                        matching_enabled_channel_ids.append(channel_id)
                        channel_to_match_priorities[str(channel_id)] = ['tvg', 'regex']
                    continue
                
                # Filter by forced_period_id if provided
                selected_period_profile = None
                if forced_period_id:
                    selected_period = next(
                        (p for p in config.get('periods', []) if p.get('id') == forced_period_id),
                        None,
                    )
                    if not selected_period:
                        continue
                    selected_period_profile = selected_period.get('profile')
                    
                profile = selected_period_profile or config.get('profile')
                
                # Check if stream matching is enabled
                matching_enabled = profile and profile.get('stream_matching', {}).get('enabled', False)
                
                # Global Action Override: Include if global action affected is True and force is True
                if force and profile and profile.get('global_action', {}).get('affected', False):
                    matching_enabled = True
                
                if matching_enabled:
                    has_regex_patterns = self.regex_matcher.has_regex_patterns(str(channel_id), effective_group_id)
                    has_tvg_matching = self.regex_matcher.get_match_by_tvg_id(str(channel_id), effective_group_id)
                    if has_regex_patterns or has_tvg_matching:
                        matching_enabled_channel_ids.append(channel_id)
                        # Store match priority order
                        channel_to_match_priorities[str(channel_id)] = profile.get('stream_matching', {}).get('match_priority_order', ['tvg', 'regex'])
                    else:
                        channel_visibility_config = self._get_channel_visibility_config(profile)
                        no_regex_visibility_enabled = bool(
                            channel_visibility_config.get("enabled")
                            and channel_visibility_config.get("hide_on_no_regex")
                        )
                        if no_regex_visibility_enabled:
                            channel_visibility_events.append(
                                self.channel_visibility_automation.handle_no_regex(
                                    channel,
                                    config=channel_visibility_config,
                                    details={
                                        "source": "stream_matching",
                                        "has_regex_patterns": False,
                                        "match_by_tvg_id": False,
                                    },
                                )
                            )
                    
                # Check if revive is enabled
                if profile and profile.get('stream_checking', {}).get('allow_revive', False):
                    channel_to_revive_enabled[str(channel_id)] = True

            # Collect channels excluded from matching but eligible for quality checking.
            #
            # These are channels whose profile has stream_matching.enabled = False but
            # stream_checking.enabled = True — "checking-only" profiles. Without this
            # collection they would silently drop out of the cycle because
            # mark_channels_updated() below only fires for channels that received new
            # stream assignments through the matching pass.
            #
            # We collect them here (before all_channels is replaced) so we can mark
            # them for the checker after the matching pass completes. They are NOT
            # added to the matching pass — that would be incorrect.
            checking_only_channel_ids = []
            for _ch in all_channels:
                _ch_id = _ch.get('id')
                if _ch_id in matching_enabled_channel_ids:
                    continue  # already handled by matching path

                # Resolve effective profile for this channel (respects forced_period_id)
                _group_id = (
                    _ch.get('group_id')
                    if _ch.get('group_id') is not None
                    else _ch.get('channel_group_id')
                )
                _config = automation_config.get_effective_configuration(_ch_id, _group_id)
                if not _config:
                    continue

                # Honour forced_period_id — only include channels that belong to that period
                if forced_period_id:
                    _periods = _config.get('periods', [])
                    _selected = next(
                        (p for p in _periods if p.get('id') == forced_period_id),
                        None,
                    )
                    if not _selected:
                        continue
                    _period_profile = _selected.get('profile')
                else:
                    _period_profile = _config.get('profile')

                _matching_disabled = not bool(
                    _period_profile
                    and _period_profile.get('stream_matching', {}).get('enabled', False)
                )
                if (
                    _period_profile
                    and _matching_disabled
                    and _period_profile.get('stream_checking', {}).get('enabled', False)
                ):
                    checking_only_channel_ids.append(_ch_id)

            if checking_only_channel_ids:
                logger.info(
                    f"Found {len(checking_only_channel_ids)} checking-only channel(s) "
                    "(matching disabled, checking enabled) — will queue for checker "
                    "after matching pass."
                )

            # Filter channels to only those with matching enabled
            filtered_channels = [ch for ch in all_channels if ch.get('id') in matching_enabled_channel_ids]
            
            excluded_count = len(all_channels) - len(filtered_channels)
            if excluded_count > 0:
                logger.info(f"Excluding {excluded_count} channel(s) without automation periods or with matching disabled from stream assignment")

            self._record_channel_visibility_events(
                channel_visibility_events,
                skip_changelog=skip_changelog,
                source="stream_matching",
            )
            
            # Use filtered channels for the rest of the logic
            all_channels = filtered_channels
            
            # Exclude channels in active monitoring sessions (coordination with monitoring system)
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_monitoring = session_manager.get_channels_in_active_sessions()
            if not isinstance(channels_in_monitoring, (list, tuple, set)):
                channels_in_monitoring = set()
            
            if channels_in_monitoring:
                pre_filter_count = len(all_channels)
                all_channels = [ch for ch in all_channels if ch.get('id') not in channels_in_monitoring]
                monitoring_excluded = pre_filter_count - len(all_channels)
                if monitoring_excluded > 0:
                    logger.info(f"⏸ Excluding {monitoring_excluded} channel(s) in active monitoring sessions from stream discovery/assignment")
            
            if not all_channels:
                logger.info("No channels available for stream assignment (all filtered or in monitoring)")
                self._mark_checking_only_channels(checking_only_channel_ids, udi, skip_check_trigger)
                return {"channel_visibility_events": channel_visibility_events}
            


            
            # Create a map of existing channel streams
            channel_streams = {}
            channel_names = {}  # Store channel names for changelog
            channel_logo_urls = {}  # Store channel logo URLs for changelog
            for channel in all_channels:
                # Validate that channel is a dictionary
                if not isinstance(channel, dict) or 'id' not in channel:
                    logger.warning(f"Invalid channel format encountered: {type(channel).__name__} - {channel}")
                    continue
                    
                channel_id = str(channel['id'])
                channel_names[channel_id] = channel.get('name', f'Channel {channel_id}')
                
                # Get logo URL for this channel
                logo_id = channel.get('logo_id')
                if logo_id:
                    channel_logo_urls[channel_id] = f"/api/logos/{logo_id}"
                
                # Get streams for this channel from UDI
                streams = udi.get_channel_streams(int(channel_id))
                if not isinstance(streams, list):
                    streams = []
                if streams:
                    valid_stream_ids = set()
                    for s in streams:
                        if isinstance(s, dict) and 'id' in s:
                            valid_stream_ids.add(s['id'])
                        else:
                            logger.warning(f"Invalid stream format in channel {channel_id}: {type(s).__name__} - {s}")
                    channel_streams[channel_id] = valid_stream_ids
                else:
                    channel_streams[channel_id] = set()
            
            assignments = defaultdict(list)
            assignment_details = defaultdict(list)  # Track stream details for changelog
            assignment_count = {}
            
            # Log progress info
            total_streams = len(all_streams)
            
            # Use parallel processing for faster matching
            # Limit workers to avoid system thrashing, but scale with streams
            # For < 1000 streams: 2-4 workers is plenty
            # For 1000-5000: 4-6 workers
            # For 5000-20000: 8 workers
            # For > 20000: Up to 16 workers (if CPU permits) for massive playlists
            base_workers = os.cpu_count() or 4
            if total_streams < 1000:
                max_workers = min(4, base_workers)
            elif total_streams < 5000:
                max_workers = min(6, base_workers)
            elif total_streams < 20000:
                max_workers = min(8, base_workers)
            else:
                max_workers = min(16, base_workers)
                
            # Batch size for streams - use more work units for large playlists
            # to improve balancing across workers and reduce long-tail batches.
            if total_streams < 1000:
                batches_per_worker = 4
            elif total_streams < 5000:
                batches_per_worker = 6
            elif total_streams < 20000:
                batches_per_worker = 10
            else:
                batches_per_worker = 16

            batch_size = max(50, total_streams // max(1, (max_workers * batches_per_worker)))
            if total_streams >= 20000:
                batch_size = min(batch_size, 400)
            
            logger.info(f"Processing {total_streams} streams for pattern matching (Parallel, {max_workers} workers, {batch_size} streams per batch)...")
            self._update_run_progress(
                stage_key="stream_matching",
                current=0,
                total=total_streams,
                message=f"Matching 0/{total_streams} streams",
            )
            if self._is_manual_stop_requested():
                message = self._manual_stop_message()
                logger.info("Stream matching skipped because the automation run stop was requested")
                self._update_run_progress(
                    stage_key="stream_matching",
                    current=0,
                    total=total_streams,
                    message=message,
                )
                return {
                    "assignment_count": {},
                    "assignment_details": [],
                    "assigned_stream_ids": {},
                    "channel_visibility_events": channel_visibility_events,
                    "aborted": True,
                    "success": False,
                    "error": message,
                }
            
            # Resolve dead stream removal setting.
            # When the caller provides allow_dead_streams explicitly (e.g. check_single_channel
            # passing the profile-resolved value), honour it directly.
            # Otherwise fall back to the global StreamCheckConfig setting.
            if allow_dead_streams is not None:
                dead_stream_removal_enabled = not allow_dead_streams
            else:
                dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
            
            # Create batches
            batches = [all_streams[i:i + batch_size] for i in range(0, total_streams, batch_size)]
            
            completed_count = 0
            last_log_pct = -1
            
            aborted = False
            future_to_batch = {}
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            try:
                future_to_batch = {
                    executor.submit(self._match_streams_batch, batch, channel_streams, 
                                   dead_stream_removal_enabled,
                                   channel_to_revive_enabled, channel_tvg_map, channel_to_match_priorities, channel_to_group_map, channel_name_map): batch 
                    for batch in batches
                }
                
                for future in concurrent.futures.as_completed(future_to_batch):
                    if self._is_manual_stop_requested():
                        aborted = True
                        break
                    try:
                        batch_assignments, batch_details = future.result()
                        if self._is_manual_stop_requested():
                            aborted = True
                            break
                        
                        # Merge results
                        for channel_id, stream_ids in batch_assignments.items():
                            assignments[channel_id].extend(stream_ids)
                            
                        for channel_id, details in batch_details.items():
                            assignment_details[channel_id].extend(details)
                            
                        completed_count += len(future_to_batch[future])
                        
                        # Log progress monotonically
                        current_pct = int((completed_count / total_streams) * 100)
                        # Log every 5% for better visibility as requested
                        if current_pct >= last_log_pct + 5 or completed_count == total_streams:
                             logger.info(f"  Progress: {completed_count}/{total_streams} streams matched ({current_pct}%)")
                             self._update_run_progress(
                                 stage_key="stream_matching",
                                 current=completed_count,
                                 total=total_streams,
                                 message=f"Matching {completed_count}/{total_streams} streams",
                             )
                             last_log_pct = current_pct
                             
                    except Exception as e:
                        logger.error(f"Error in stream matching batch: {e}")
            finally:
                if aborted:
                    for pending in future_to_batch:
                        pending.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                else:
                    executor.shutdown(wait=True)

            if aborted:
                message = self._manual_stop_message()
                logger.info(
                    f"Stream matching aborted after {completed_count}/{total_streams} streams"
                )
                self._update_run_progress(
                    stage_key="stream_matching",
                    current=completed_count,
                    total=total_streams,
                    message=message,
                )
                return {
                    "assignment_count": {},
                    "assignment_details": [],
                    "assigned_stream_ids": {},
                    "channel_visibility_events": channel_visibility_events,
                    "aborted": True,
                    "success": False,
                    "error": message,
                }
            
            logger.info(f"✓ Completed processing {total_streams} streams. Found {sum(len(s) for s in assignments.values())} new stream assignments across {len(assignments)} channels")
            
            # Get channels in active monitoring sessions to handle them separately
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_sessions = session_manager.get_channels_in_active_sessions()
            
            if channels_in_sessions:
                logger.info(f"Found {len(channels_in_sessions)} channel(s) in active monitoring sessions - will add streams to sessions instead of direct assignment")
            
            # Prepare detailed changelog data
            detailed_assignments = []
            
            # dead_stream_removal_enabled was already resolved above; reuse it here.
            # (allow_dead_streams caller override is already reflected in the variable.)
            
            # Assign streams to channels
            for channel_id, stream_ids in assignments.items():
                if stream_ids:
                    try:
                        channel_id_int = int(channel_id)
                        
                        # Check if channel is in an active monitoring session
                        if channel_id_int in channels_in_sessions:
                            # Add streams to the active session(s) instead of direct channel assignment
                            logger.info(f"Channel {channel_id} is in an active monitoring session - adding {len(stream_ids)} streams to session(s)")
                            added_to_session = 0
                            
                            # Find all active sessions for this channel
                            active_sessions = [s for s in session_manager.get_active_sessions() if s.channel_id == channel_id_int]
                            
                            for session in active_sessions:
                                for stream_id in stream_ids:
                                    if session_manager.add_stream_to_session(session.session_id, stream_id):
                                        added_to_session += 1
                            
                            if added_to_session > 0:
                                logger.info(f"Added {added_to_session} new streams to monitoring session(s) for channel {channel_id}")
                                assignment_count[channel_id] = added_to_session
                                
                                # Prepare detailed assignment info
                                channel_assignment = {
                                    "channel_id": channel_id,
                                    "channel_name": channel_names.get(channel_id, f'Channel {channel_id}'),
                                    "logo_url": channel_logo_urls.get(channel_id),
                                    "stream_count": added_to_session,
                                    "streams": assignment_details[channel_id][:20],  # Limit to first 20 for changelog
                                    "added_to_session": True
                                }
                                detailed_assignments.append(channel_assignment)
                            continue
                        
                        # Normal channel assignment (not in session)
                        # When allow_revive is enabled for this channel, pass allow_dead_streams=True
                        # so that previously-dead streams that matched are actually added back to
                        # the channel. Without this, _match_streams_batch correctly lets them
                        # through the revive filter but add_streams_to_channel (and the
                        # update_channel_streams it calls internally) would filter them out again
                        # via filter_dead_streams, making allow_revive permanently ineffective.
                        _ch_revive_enabled = channel_to_revive_enabled.get(channel_id, False)
                        _ch_allow_dead = (not dead_stream_removal_enabled) or _ch_revive_enabled
                        try:
                            added_count = assign_streams_to_channel(channel_id_int, stream_ids, allow_dead_streams=_ch_allow_dead)
                        except TypeError as assign_error:
                            if "allow_dead_streams" not in str(assign_error):
                                raise
                            added_count = assign_streams_to_channel(channel_id_int, stream_ids)
                        assignment_count[channel_id] = added_count
                        
                        # Verify streams were added correctly (if enabled in config)
                        verify_enabled = self.config.get('verify_stream_assignments', False)
                        if added_count > 0 and verify_enabled:
                            try:
                                time.sleep(0.5)  # Brief delay for API processing
                                # Refresh this specific channel in UDI to get updated data after write
                                udi.refresh_channel_by_id(int(channel_id))
                                updated_channel = udi.get_channel_by_id(int(channel_id))
                                
                                if updated_channel:
                                    updated_stream_ids = set(updated_channel.get('streams', []))
                                    expected_stream_ids = set(stream_ids)
                                    added_stream_ids = expected_stream_ids & updated_stream_ids
                                    
                                    if len(added_stream_ids) == added_count:
                                        logger.info(f"✓ Verified: {added_count} streams successfully added to channel {channel_id} ({channel_names.get(channel_id, f'Channel {channel_id}')})")
                                    else:
                                        logger.warning(f"⚠ Verification mismatch for channel {channel_id}: expected {added_count} streams, found {len(added_stream_ids)} in channel")
                                else:
                                    logger.warning(f"⚠ Could not verify stream addition for channel {channel_id}: channel not found")
                            except Exception as verify_error:
                                logger.warning(f"⚠ Could not verify stream addition for channel {channel_id}: {verify_error}")
                        elif added_count > 0:
                            logger.debug(f"Skipped verification for channel {channel_id} (disabled in config)")
                        
                        if added_count > 0:
                            channel_assignment = {
                                "channel_id": channel_id,
                                "channel_name": channel_names.get(channel_id, f'Channel {channel_id}'),
                                "logo_url": channel_logo_urls.get(channel_id),
                                "stream_count": added_count,
                                "streams": assignment_details[channel_id][:20],  # Limit to first 20 for changelog
                                "added_to_session": False
                            }
                            detailed_assignments.append(channel_assignment)
                        
                        
                    except Exception as e:
                        logger.error(f"Failed to assign streams to channel {channel_id}: {e}")
            
            accepted_assignment_count = {
                channel_id: count
                for channel_id, count in assignment_count.items()
                if count > 0
            }
            total_assigned = sum(accepted_assignment_count.values())
            accepted_channel_count = len(accepted_assignment_count)
            # Add comprehensive changelog entry
            if self.config.get("enabled_features", {}).get("changelog_tracking", True) and not skip_changelog:
                # Limit detailed assignments to prevent oversized changelog entries
                # Sort by stream count (descending) to show the most significant updates
                sorted_assignments = sorted(detailed_assignments, key=lambda x: x['stream_count'], reverse=True)
                max_channels_in_changelog = 50  # Limit to 50 channels to prevent performance issues
                
                self.changelog.add_entry("streams_assigned", {
                    "total_assigned": total_assigned,
                    "channel_count": accepted_channel_count,
                    "assignments": sorted_assignments[:max_channels_in_changelog],
                    "has_more_channels": len(sorted_assignments) > max_channels_in_changelog,
                    "timestamp": datetime.now().isoformat()
                })
            
            logger.info(f"Stream discovery completed. Assigned {total_assigned} new streams across {accepted_channel_count} channels")
            
            # Mark channels that received new streams for stream quality checking
            if total_assigned > 0 and accepted_assignment_count:
                try:
                    # Get updated stream counts for channels that received new streams
                    channel_ids_to_mark = []
                    stream_counts = {}
                    
                    for channel_id, added_count in accepted_assignment_count.items():
                        channel_ids_to_mark.append(int(channel_id))
                        # Avoid a post-write UDI/network fetch here. The checker
                        # only needs a best-effort current count for its update
                        # tracker, and we already know the pre-assignment set plus
                        # how many streams Dispatcharr accepted.
                        existing_streams = channel_streams.get(str(channel_id), set())
                        stream_counts[int(channel_id)] = len(existing_streams) + int(added_count)
                    
                    # Try to get stream checker service and mark channels
                    if channel_ids_to_mark:
                        try:
                            from apps.stream.stream_checker_service import get_stream_checker_service
                            stream_checker = get_stream_checker_service()
                            stream_checker.update_tracker.mark_channels_updated(channel_ids_to_mark, stream_counts=stream_counts)
                            logger.info(f"Marked {len(channel_ids_to_mark)} channels with new streams for stream quality checking")
                            # Trigger immediate check instead of waiting for scheduled interval
                            # Skip if caller will handle the check (e.g., check_single_channel)
                            if not skip_check_trigger:
                                stream_checker.trigger_check_updated_channels()
                            else:
                                logger.debug("Skipping automatic check trigger (will be handled by caller)")
                        except Exception as sc_error:
                            logger.debug(f"Stream checker not available or error marking channels: {sc_error}")
                except Exception as mark_error:
                    logger.debug(f"Could not mark channels for stream checking after discovery: {mark_error}")
            
            # Mark checking-only channels for quality checking.
            #
            # These channels had stream_matching.enabled = False so they were excluded
            # from the matching pass and never received a mark_channels_updated() call.
            # Without this block they are silently skipped by the checker every cycle.
            # The existing get_and_clear_channels_needing_check() already filters by
            # stream_checking.enabled so no duplicate guard is needed here.
            if checking_only_channel_ids:
                self._mark_checking_only_channels(checking_only_channel_ids, udi, skip_check_trigger)
            
            accepted_assigned_stream_ids = {
                str(channel_id): stream_ids
                for channel_id, stream_ids in assignments.items()
                if accepted_assignment_count.get(str(channel_id), 0) > 0
            }

            return {
                "assignment_count": accepted_assignment_count,
                "assignment_details": detailed_assignments,
                "assigned_stream_ids": dict(accepted_assigned_stream_ids),
                "channel_visibility_events": channel_visibility_events,
            }
            
        except Exception as e:
            logger.error(f"Stream discovery failed: {e}")
            return {
                "assignment_count": {},
                "assignment_details": [],
                "assigned_stream_ids": {},
                "success": False,
                "error": str(e)
            }
    
    def validate_and_remove_non_matching_streams(self, force: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Validate existing streams in channels against regex patterns.
        Remove streams that no longer match their channel's patterns.
        
        This function respects the automation_controls.remove_non_matching_streams setting
        unless force=True is passed. This ensures consistent behavior across:
        - Automation cycles (step 1.5 in the pipeline)
        - Single channel checks
        - Global actions
        
        Args:
            force: If True, bypass the automation_controls config check.
                   Reserved for future use or special cases where removal must happen
                   regardless of user settings. Default is False to respect user config.
            forced_period_id: Optional period ID to filter channels.
        
        Returns:
            Dict containing validation statistics:
            - channels_checked: Number of channels checked
            - streams_removed: Total streams removed
            - channels_modified: Number of channels that had streams removed
            - details: List of channel details with removed streams
        """
        log_function_call(logger, "validate_and_remove_non_matching_streams")
        
        # Check if removal is enabled in stream checker config (unless forced)
        if not force:
            try:
                from apps.stream.stream_checker_service import get_stream_checker_service
                stream_checker = get_stream_checker_service()
                removal_enabled = stream_checker.config.get('automation_controls', {}).get('remove_non_matching_streams', False)
                
                if not removal_enabled:
                    logger.debug("Stream removal is disabled in automation_controls")
                    return {
                        "channels_checked": 0,
                        "streams_removed": 0,
                        "channels_modified": 0,
                        "details": []
                    }
            except Exception as e:
                logger.warning(f"Could not check stream checker config: {e}, skipping validation")
                return {
                    "channels_checked": 0,
                    "streams_removed": 0,
                    "channels_modified": 0,
                    "details": []
                }
        
        # Lock to prevent concurrent execution
        if not self._lock.acquire(blocking=False):
            logger.warning("Stream validation already active - skipping concurrent request")
            return {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": []
            }
            
        try:
            return self._validate_and_remove_non_matching_streams_impl(force, forced_period_id, skip_changelog, channel_id)
        finally:
            self._lock.release()

    def _validate_and_remove_non_matching_streams_impl(self, force: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None) -> Dict[str, Any]:
        """Core implementation of stream validation."""
        log_function_call(logger, "validate_and_remove_non_matching_streams")
        try:
            logger.info("=" * 80)
            
            udi = get_udi_manager()
            all_channels = udi.get_channels()
            
            if not all_channels:
                logger.info("No channels found")
                return False, []
            
            # Filter by profile if one is selected
            all_channels = self._filter_channels_by_profile(all_channels, "stream validation")

            # Scope to a single channel when called from check_single_channel.
            if channel_id is not None:
                all_channels = [ch for ch in all_channels if ch.get('id') == channel_id]
                if not all_channels:
                    logger.info(f"[single-channel] Channel {channel_id} not found or not eligible for stream validation")
                    return {
                        "channels_checked": 0,
                        "streams_removed": 0,
                        "channels_modified": 0,
                        "details": []
                    }
                logger.info(f"[single-channel] Scoping stream validation to channel {channel_id}")

            # Filter channels with matching enabled using Automation Profiles
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            
            matching_enabled_channel_ids = []
            channel_validation_settings = {}
            for channel in all_channels:
                channel_id = channel.get('id')
                channel_group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                
                # Get effective configuration - only channels with automation periods participate
                config = automation_config.get_effective_configuration(channel_id, channel_group_id)
                
                # Skip channels without automation periods assigned
                if not config:
                    continue
                
                # Filter by forced_period_id if provided
                selected_period_profile = None
                if forced_period_id:
                    selected_period = next(
                        (p for p in config.get('periods', []) if p.get('id') == forced_period_id),
                        None,
                    )
                    if not selected_period:
                        continue
                    selected_period_profile = selected_period.get('profile')
                        
                profile = selected_period_profile or config.get('profile')
                
                # Check if stream matching is enabled in the profile
                matching_enabled = profile and profile.get('stream_matching', {}).get('enabled', False)
                validate_enabled = profile and profile.get('stream_matching', {}).get('validate_existing_streams', False)
                
                # Global Action Override: Include if global action affected is True and force is True
                if force and profile and profile.get('global_action', {}).get('affected', False):
                    matching_enabled = True
                    validate_enabled = True # Force validation if global action dictates
                    
                if matching_enabled:
                    matching_enabled_channel_ids.append(channel_id)
                    channel_validation_settings[channel_id] = {
                        "validate_enabled": validate_enabled
                    }

            # Legacy fallback: before automation profiles existed, validation
            # applied to channels that simply had regex/TVG matching configured.
            # Keep that behavior for old scripts/tests when no profile selected
            # any channels.
            if not matching_enabled_channel_ids:
                for channel in all_channels:
                    legacy_channel_id = channel.get('id')
                    legacy_group_id = (
                        channel.get('group_id')
                        if channel.get('group_id') is not None
                        else channel.get('channel_group_id')
                    )
                    if (
                        self.regex_matcher.has_regex_patterns(str(legacy_channel_id), legacy_group_id)
                        or self.regex_matcher.get_match_by_tvg_id(str(legacy_channel_id), legacy_group_id)
                    ):
                        matching_enabled_channel_ids.append(legacy_channel_id)
                        channel_validation_settings[legacy_channel_id] = {
                            "validate_enabled": True
                        }
            
            # Exclude channels in active monitoring sessions (coordination with monitoring system)
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_monitoring = session_manager.get_channels_in_active_sessions()
            
            if channels_in_monitoring:
                # Filter out channels in monitoring from matching_enabled list
                pre_filter_count = len(matching_enabled_channel_ids)
                matching_enabled_channel_ids = [ch_id for ch_id in matching_enabled_channel_ids if ch_id not in channels_in_monitoring]
                monitoring_excluded = pre_filter_count - len(matching_enabled_channel_ids)
                if monitoring_excluded > 0:
                    logger.info(f"⏸ Excluding {monitoring_excluded} channel(s) in active monitoring sessions from stream validation")
            
            validation_results = {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": []
            }
            
            # Get dead stream removal setting to pass to update_channel_streams
            # This ensures the setting is respected when validating streams
            dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
            
            # Get all streams from UDI for lookup
            all_streams = udi.get_streams(log_result=False)
            stream_lookup = {s['id']: s for s in all_streams if isinstance(s, dict) and 'id' in s}
            
            # Build the full TVG-ID map from ALL channels upfront so that cross-batch TVG
            # lookups inside _validate_channels_batch work correctly (Bug 2 fix).
            full_channel_tvg_map = {
                str(ch.get('id')): ch.get('tvg_id')
                for ch in all_channels
                if isinstance(ch, dict) and ch.get('tvg_id')
            }
            
            # Parallel validation for faster processing
            # Limit workers to avoid system thrashing.
            max_workers = min(8, os.cpu_count() or 4)
            # Batch size for channels - smaller than streams since per-channel work is heavier
            batch_size = max(20, len(all_channels) // (max_workers * 2))
            
            logger.info(f"Validating {len(all_channels)} channels (Parallel, {max_workers} workers)...")
            
            # Create batches
            batches = [all_channels[i:i + batch_size] for i in range(0, len(all_channels), batch_size)]
            
            completed_count = 0
            
            aborted = False
            future_to_batch = {}
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            try:
                future_to_batch = {
                    executor.submit(self._validate_channels_batch, batch, stream_lookup,
                                    matching_enabled_channel_ids, channel_validation_settings,
                                    full_channel_tvg_map): batch 
                    for batch in batches
                }
                
                for future in concurrent.futures.as_completed(future_to_batch):
                    if self._is_manual_stop_requested():
                        aborted = True
                        break
                    try:
                        batch_results = future.result()
                        if self._is_manual_stop_requested():
                            aborted = True
                            break
                        
                        validation_results["channels_checked"] += batch_results["channels_checked"]
                        
                        # Process results and update UDI if needed
                        for detail in batch_results.get("details", []):
                            if self._is_manual_stop_requested():
                                aborted = True
                                break
                            channel_id = detail["channel_id"]
                            channel_name = detail["channel_name"]
                            kept_ids = detail["kept_ids"]
                            removed_streams = detail["removed_streams"]
                            
                            # Only apply updates if this channel's profile has validate_existing_streams
                            # enabled, OR if this is a forced run (e.g. global action).
                            # Bug 3 fix: previously gated on dead_stream_removal_enabled which ignored
                            # the per-profile validate_enabled setting entirely.
                            channel_validate_enabled = detail.get("validate_enabled", False)
                            if channel_validate_enabled or force:
                                try:
                                    # Update channel with kept streams
                                    success = update_channel_streams(channel_id, kept_ids, allow_dead_streams=(not dead_stream_removal_enabled))
                                    
                                    if success:
                                        validation_results["streams_removed"] += len(removed_streams)
                                        validation_results["channels_modified"] += 1
                                        validation_results["details"].append(detail)
                                        
                                        logger.info(f"✓ Removed {len(removed_streams)} non-matching stream(s) from {channel_name}")
                                    else:
                                        logger.error(f"Failed to update channel {channel_name} after validation")
                                        
                                except Exception as update_err:
                                    logger.error(f"Failed to update channel {channel_id}: {update_err}")
                            else:
                                if len(removed_streams) > 0:
                                    # Log but don't apply — validate_existing_streams is disabled for this channel
                                    logger.debug(f"Found {len(removed_streams)} non-matching streams in channel {channel_id}, but validate_existing_streams is disabled for its profile")
                        
                        completed_count += len(future_to_batch[future])
                        # Log less frequently
                        if completed_count == len(all_channels):
                             logger.info(f"  Validation progress: 100%")
                        elif completed_count % 100 == 0:
                             logger.info(f"  Validation progress: {int(completed_count/len(all_channels)*100)}%")

                    except Exception as e:
                        logger.error(f"Error in channel validation batch: {e}")
                    if aborted:
                        break
            finally:
                if aborted:
                    for pending in future_to_batch:
                        pending.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                else:
                    executor.shutdown(wait=True)

            if aborted:
                validation_results["aborted"] = True
                validation_results["success"] = False
                validation_results["error"] = self._manual_stop_message()
                logger.info(
                    "Stream validation aborted after checking "
                    f"{validation_results['channels_checked']} channel(s)"
                )
                return validation_results
            
            logger.info(f"Stream validation completed: Checked {validation_results['channels_checked']} channels, " +
                       f"removed {validation_results['streams_removed']} streams from {validation_results['channels_modified']} channels")
            
            # Add changelog entry if there were changes
            if validation_results['streams_removed'] > 0 and self.config.get("enabled_features", {}).get("changelog_tracking", True) and not skip_changelog:
                self.changelog.add_entry("stream_validation", {
                    "channels_checked": validation_results['channels_checked'],
                    "streams_removed": validation_results['streams_removed'],
                    "channels_modified": validation_results['channels_modified'],
                    "timestamp": datetime.now().isoformat()
                })
            
            return validation_results
            
        except Exception as e:
            logger.error(f"Stream validation failed: {e}", exc_info=True)
            return {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": [],
                "error": str(e)
            }
    
    
    def _get_missed_run_grace_minutes(self, period_info: dict) -> int:
        try:
            grace_minutes = int(period_info.get("missed_run_grace_minutes") or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, grace_minutes)

    def _record_period_skip(
        self,
        period_id: str,
        period_info: dict,
        *,
        reason: str,
        due_at: Optional[datetime] = None,
        now: datetime,
        grace_minutes: int,
        message: Optional[str] = None,
    ) -> None:
        if not hasattr(self, "_period_skip_history") or not isinstance(self._period_skip_history, dict):
            self._period_skip_history = {}

        entry = {
            "reason": reason,
            "period_id": str(period_id),
            "period_name": period_info.get("name") or str(period_id),
            "due_at": due_at.isoformat() if isinstance(due_at, datetime) else None,
            "skipped_at": now.isoformat(),
            "grace_minutes": max(0, int(grace_minutes or 0)),
            "message": message or "Missed-run grace expired before the scheduler observed this period",
        }
        history = [entry] + list(self._period_skip_history.get(str(period_id), []))
        self._period_skip_history[str(period_id)] = history[:10]

    def get_period_skip_history(self, period_id: Optional[str] = None, *, limit: int = 10) -> Any:
        if not hasattr(self, "_period_skip_history") or not isinstance(self._period_skip_history, dict):
            self._period_skip_history = {}

        try:
            normalized_limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            normalized_limit = 10

        if period_id is not None:
            return list(self._period_skip_history.get(str(period_id), []))[:normalized_limit]

        return {
            str(pid): list(entries)[:normalized_limit]
            for pid, entries in self._period_skip_history.items()
        }

    def _get_catch_up_max_periods_per_cycle(self, global_settings: dict) -> int:
        try:
            cap = int(global_settings.get("catch_up_max_periods_per_cycle") or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, cap)

    def _parse_policy_time(self, value: Any) -> Optional[tuple[int, int]]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except (TypeError, ValueError):
            return None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    def _is_maintenance_window_active(self, global_settings: dict, now: Optional[datetime] = None) -> bool:
        if not global_settings.get("maintenance_window_enabled"):
            return False
        start = self._parse_policy_time(global_settings.get("maintenance_window_start"))
        end = self._parse_policy_time(global_settings.get("maintenance_window_end"))
        if start is None or end is None or start == end:
            return False

        current = now or datetime.now()
        current_minutes = current.hour * 60 + current.minute
        start_minutes = start[0] * 60 + start[1]
        end_minutes = end[0] * 60 + end[1]
        if start_minutes < end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    def _get_teamarr_event_window_minutes(self, global_settings: dict, key: str, default: int) -> int:
        try:
            value = int(global_settings.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(0, min(value, 1440))

    def _get_active_teamarr_event_window(self, global_settings: dict, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        if not global_settings.get("teamarr_event_window_enabled"):
            return None

        before_minutes = self._get_teamarr_event_window_minutes(
            global_settings,
            "teamarr_event_window_before_minutes",
            30,
        )
        after_minutes = self._get_teamarr_event_window_minutes(
            global_settings,
            "teamarr_event_window_after_minutes",
            10,
        )
        if before_minutes <= 0 and after_minutes <= 0:
            return None

        try:
            from apps.stream.teamarr_preflight_service import get_teamarr_preflight_service
            status = get_teamarr_preflight_service().get_status()
        except Exception as exc:
            logger.debug("Could not read Teamarr event window status: %s", exc)
            return None

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)

        for event in status.get("upcoming_events") or []:
            if not isinstance(event, dict):
                continue
            if not event.get("dispatcharr_channel_id"):
                continue

            state = str(event.get("state") or "").strip().lower()
            if state in {"filtered", "past", "no_dispatcharr_channel"}:
                continue

            event_date = event.get("event_date")
            try:
                event_at = datetime.fromisoformat(str(event_date).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=timezone.utc)

            window_start = event_at - timedelta(minutes=before_minutes)
            window_end = event_at + timedelta(minutes=after_minutes)
            if window_start <= current <= window_end:
                return {
                    "event_name": event.get("event_name") or event.get("channel_name") or "Teamarr event",
                    "channel_name": event.get("channel_name"),
                    "event_date": event_at.isoformat(),
                    "state": state or None,
                    "seconds_to_start": int((event_at - current).total_seconds()),
                    "window_before_minutes": before_minutes,
                    "window_after_minutes": after_minutes,
                }

        return None

    def _apply_global_catch_up_cap(
        self,
        active_periods: Dict[tuple, dict],
        global_settings: dict,
        *,
        forced: bool,
    ) -> Dict[tuple, dict]:
        if forced:
            return active_periods
        run_all_due_periods = bool(global_settings.get("run_all_due_periods", False))
        cap = self._get_catch_up_max_periods_per_cycle(global_settings) if run_all_due_periods else 1
        if run_all_due_periods and cap <= 0:
            return active_periods
        if len(active_periods) <= cap:
            return active_periods

        def sort_key(item):
            (period_id, period_name), data = item
            try:
                numeric_period_id = int(period_id)
            except (TypeError, ValueError):
                numeric_period_id = 0
            return (-int(data.get("priority") or 0), numeric_period_id, str(period_name))

        ordered = sorted(active_periods.items(), key=sort_key)
        kept = dict(ordered[:cap])
        skipped = ordered[cap:]
        now = datetime.now()
        skip_reason = "global_catch_up_cap" if run_all_due_periods else "run_all_due_disabled"
        skip_message = (
            "Global catch-up cap deferred this period to the next scheduler pass"
            if run_all_due_periods
            else "Run all due periods is disabled; this period is deferred to the next scheduler pass"
        )
        for (period_id, _period_name), data in skipped:
            self._record_period_skip(
                str(period_id),
                {
                    "id": str(period_id),
                    "name": data.get("period_name") or str(period_id),
                },
                reason=skip_reason,
                now=now,
                grace_minutes=0,
                message=skip_message,
            )
        self._save_state()
        logger.info(
            "Automatic due-period policy kept %s period(s) and deferred %s period(s) "
            "(run_all_due_periods=%s, cap=%s)",
            len(kept),
            len(skipped),
            run_all_due_periods,
            cap,
        )
        return kept

    def _period_due_inside_grace(
        self,
        period_id: str,
        period_info: dict,
        due_at: datetime,
        now: datetime,
    ) -> bool:
        if now < due_at:
            return False

        grace_minutes = self._get_missed_run_grace_minutes(period_info)
        if grace_minutes <= 0:
            return True

        grace_until = due_at + timedelta(minutes=grace_minutes)
        if now <= grace_until:
            return True

        self.period_last_run[period_id] = now
        self._record_period_skip(
            period_id,
            period_info,
            reason="missed_run_grace_expired",
            due_at=due_at,
            now=now,
            grace_minutes=grace_minutes,
        )
        self._save_state()
        logger.info(
            "Skipping missed automation period %s because its grace window expired "
            "(due_at=%s, grace_minutes=%s, now=%s)",
            period_id,
            due_at.isoformat(),
            grace_minutes,
            now.isoformat(),
        )
        return False

    def _is_period_due(self, period_id: str, period_info: dict) -> bool:
        """Check if a specific period is due to run based on its schedule."""
        now = datetime.now()
        last_run = self.period_last_run.get(period_id)
        if not last_run:
            if period_info.get("catch_up_missed_runs", False):
                logger.info(
                    f"Period {period_id} has no last_run timestamp and startup catch-up is enabled; "
                    "running once on this scheduler pass"
                )
                return True

            # If no previous run, initialize to now() so it waits for the first interval/cron schedule block
            self.period_last_run[period_id] = now
            logger.info(f"Initialized last_run for period {period_id} to now() to wait for next schedule run")
            return False
            
        schedule = period_info.get("schedule", {})
        schedule_type = schedule.get("type", "interval")
        
        if schedule_type == "interval":
            try:
                interval_mins = int(schedule.get("value", 60))
            except ValueError:
                logger.warning(f"Invalid interval value for period {period_id}, using default 60")
                interval_mins = 60
            due_at = last_run + timedelta(minutes=interval_mins)
            return self._period_due_inside_grace(period_id, period_info, due_at, now)
        elif schedule_type == "cron" and CRONITER_AVAILABLE:
            try:
                cron = croniter(schedule.get("value"), last_run)
                next_run = cron.get_next(datetime)
                return self._period_due_inside_grace(period_id, period_info, next_run, now)
            except Exception as e:
                logger.error(
                    f"Invalid cron expression for period {period_id} "
                    f"(value={schedule.get('value')!r}): {e} — period will be skipped until the "
                    f"expression is corrected."
                )
                return False
        elif schedule_type == "cron":
            logger.error(
                f"croniter is not installed — cron schedule for period {period_id} cannot be "
                f"evaluated. Period will be skipped. Install croniter to enable cron support."
            )
            return False
             
        return False

    def _refresh_udi_cache_for_automation_cycle(self) -> bool:
        """Refresh all UDI entities after an automation cycle completes.

        Called in a background daemon thread from the finally block of
        run_automation_cycle(). Pulls all writes made during the cycle
        (stream assignments, quality scores, stream ordering) back into
        the local cache so the next cycle or single-channel check starts
        from an accurate baseline.

        This is intentionally post-cycle rather than pre-cycle so that
        the cycle itself operates entirely on the existing cache state —
        fast and deterministic regardless of UDI sync timing.
        """
        try:
            logger.info("Refreshing UDI cache for automation cycle...")
            udi = get_udi_manager()
            udi.refresh_m3u_accounts()
            udi.refresh_streams()
            udi.refresh_channels()
            udi.refresh_channel_groups()
            udi.refresh_channel_profiles()

            logger.info("UDI cache refresh for automation cycle completed")
            return True
        except Exception as e:
            logger.error(f"Failed to refresh UDI cache for automation cycle: {e}")
            return False

    def _get_post_refresh_delay_seconds(self) -> float:
        """Return optional delay after playlist refresh before matching starts.

        Priority order:
        1) Environment variable `STREAMFLOW_POST_REFRESH_DELAY_SECONDS`
        2) Config key `post_refresh_delay_seconds`
        3) Config key `automation_tuning.post_refresh_delay_seconds`
        4) Default `0.0` (no artificial delay)
        """
        env_value = os.environ.get("STREAMFLOW_POST_REFRESH_DELAY_SECONDS")
        if env_value is not None:
            try:
                return max(0.0, float(env_value))
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid STREAMFLOW_POST_REFRESH_DELAY_SECONDS='{env_value}', using config/default"
                )

        configured_value = self.config.get("post_refresh_delay_seconds")
        if configured_value is None:
            configured_value = self.config.get("automation_tuning", {}).get("post_refresh_delay_seconds")

        if configured_value is None:
            return 0.0

        try:
            return max(0.0, float(configured_value))
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid post_refresh_delay_seconds value '{configured_value}', defaulting to 0"
            )
            return 0.0
    
    def run_automation_cycle(self, forced: bool = False, forced_period_id: str = None):
        """Run one complete automation cycle with profile support."""
        self._start_run_status(forced=forced, forced_period_id=forced_period_id)
        # Determine if this is a forced run (manual trigger)
        # forced and forced_period_id are now passed as arguments
        if forced:
            logger.info(f"Forcing automation cycle{' for period ' + forced_period_id if forced_period_id else ''}")
        self._update_run_status(
            stage="settings",
            stage_label="Preparing Automation",
            message="Reading automation configuration",
        )
        if self._abort_run_if_manual_stop_requested():
            return
            
        # 1. Check Global Automation Switch
        from apps.automation.automation_config_manager import get_automation_config_manager
        automation_config = get_automation_config_manager()
        global_settings = automation_config.get_global_settings()
        
        legacy_config_file_mode = (
            getattr(self, "_explicit_config_file", False)
            or Path(CONFIG_DIR) != Path('/app/data')
        )
        current_enabled = global_settings.get('regular_automation_enabled', False) or legacy_config_file_mode
        
        # Initialize flag if missing
        if not hasattr(self, '_was_automation_enabled'):
            self._was_automation_enabled = current_enabled
            
        # If just enabled, reset period timers to wait for the next scheduled block
        if current_enabled and not self._was_automation_enabled:
            logger.info("Automation system just enabled, resetting period last run states to wait")
            self.period_last_run.clear()
            
        self._was_automation_enabled = current_enabled
        
        if not forced and not current_enabled:
            logger.debug("Regular automation is disabled globally. Skipping cycle.")
            self._finish_run_status(
                state="skipped",
                stage="skipped",
                stage_label="Skipped",
                message="Regular automation is disabled",
            )
            return

        if not forced and self._is_maintenance_window_active(global_settings):
            logger.debug("Maintenance window is active. Skipping automation cycle.")
            self._finish_run_status(
                state="skipped",
                stage="skipped",
                stage_label="Skipped",
                message="Maintenance window is active",
            )
            return

        teamarr_event_window = self._get_active_teamarr_event_window(global_settings)
        if not forced and not forced_period_id and teamarr_event_window:
            event_name = teamarr_event_window.get("event_name") or "Teamarr event"
            logger.info(
                "Teamarr event window is active for %s. Skipping automatic automation cycle.",
                event_name,
            )
            self._update_run_status(
                counts={"teamarr_event_window_active": True},
                message=f"Teamarr event window active for {event_name}",
            )
            self._finish_run_status(
                state="skipped",
                stage="skipped",
                stage_label="Skipped",
                message=f"Teamarr event window active for {event_name}",
            )
            return

        # Check if stream checking mode is active. Full automation must not run
        # next to single-channel checks or preflight work, but the run intent
        # should remain pending so it can start when the checker becomes idle.
        try:
            stream_checker = get_stream_checker_service()
            status = stream_checker.get_status()
            if status.get('stream_checking_mode', False):
                if forced:
                    self.force_next_run = True
                    self.forced_period_id = forced_period_id
                    logger.info(
                        "Stream checking is active. Queuing forced automation cycle%s.",
                        f" for period {forced_period_id}" if forced_period_id else "",
                    )
                else:
                    logger.info("Stream checking is active. Deferring automation cycle until checker is idle.")
                self._queue_run_status("Stream checker is active; automation run is queued")
                return
        except Exception as e:
            logger.debug(f"Could not check stream checking mode status: {e}")
        # Global setting for playlist updates is now period-driven
        # We don't early return here, we let the individual periods be checked below

        if legacy_config_file_mode:
            try:
                if self.last_playlist_update is None or forced:
                    self.refresh_playlists()
                    self.discover_and_assign_streams(force=True)
                self._finish_run_status(
                    state="completed",
                    stage="finalizing",
                    stage_label="Finalizing",
                    message="Legacy automation cycle completed",
                )
                return
            finally:
                self._m3u_accounts_cache = None
        
        logger.debug("Starting automation cycle...")
        automation_busy_guard = get_udi_manager()
        automation_busy_guard.set_automation_busy()

        try:
            if self._abort_run_if_manual_stop_requested():
                return

            # 2. Determine which playlists to update and group channels by period
            self._update_run_status(
                stage="period_discovery",
                stage_label="Checking Schedule",
                message="Loading scheduled windows and channel assignments",
            )
            udi = automation_busy_guard
            channels = udi.get_channels()
            channels_by_id = {
                int(channel.get('id')): channel
                for channel in channels
                if isinstance(channel, dict) and channel.get('id') is not None
            }
            active_periods = {} # {(period_id, period_name): {profile_id, profile_name, channels: []}}
            active_profile_ids = set()
            
            for channel in channels:
                channel_id = channel.get('id')
                # Resolve group key from either UDI shape to include group-only period assignments.
                effective_group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                # Get effective configuration - only channels with automation periods participate
                config = automation_config.get_effective_configuration(channel_id, effective_group_id)
                if config and config.get('periods'):
                    for period_info in config['periods']:
                        p_id = period_info.get('id')
                        if forced_period_id and p_id != forced_period_id:
                            continue
                            
                        # Check if the period is actually due
                        if not forced and not forced_period_id and not self._is_period_due(p_id, period_info):
                            continue
                            
                        p_name = period_info.get('name')
                        profile = period_info.get('profile')
                        profile_id = period_info.get('profile_id')
                        if profile_id is not None:
                            profile_id = str(profile_id)
                        
                        if p_id and p_name:
                            key = (p_id, p_name)
                            if key not in active_periods:
                                active_periods[key] = {
                                    'profile_id': profile_id,
                                    'profile_name': profile.get('name') if profile else "Default",
                                    'period_name': p_name,
                                    'priority': period_info.get('priority', 0),
                                    'channels': []
                                }
                            active_periods[key]['channels'].append(channel)
                            if profile_id:
                                active_profile_ids.add(profile_id)

            active_periods = self._apply_global_catch_up_cap(
                active_periods,
                global_settings,
                forced=bool(forced or forced_period_id),
            )
            active_profile_ids = {
                str(entry.get('profile_id'))
                for entry in active_periods.values()
                if entry.get('profile_id') is not None
            }
            self._finalize_run_snapshot(
                active_periods,
                automation_config,
                global_settings,
                udi=udi,
                teamarr_event_window=teamarr_event_window,
            )

            if not active_periods:
                logger.debug("No channels with active automation periods found. Skipping cycle.")
                self.last_playlist_update = datetime.now()
                self._finish_run_status(
                    state="skipped",
                    stage="skipped",
                    stage_label="Skipped",
                    message="No active automation periods were due",
                )
                return
            
            channels_with_periods = sum(len(p['channels']) for p in active_periods.values())
            self._update_run_status(
                counts={
                    "active_periods": len(active_periods),
                    "channels_with_periods": channels_with_periods,
                },
                message=f"Found {channels_with_periods} channel assignments across {len(active_periods)} active period(s)",
            )
            logger.info(f"Processing {channels_with_periods} channel assignments across {len(active_periods)} active period(s)")
            logger.info(f"UDI cache {udi.get_cache_age_description()}")
            if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                return
            
            # Determine playlists to update
            playlists_to_update = set()
            update_all_playlists = False
            channels_to_quality_check = []
            channel_check_all_streams = {}
            
            for p_id in active_profile_ids:
                profile = automation_config.get_profile(p_id)
                if not profile: continue
                
                if profile.get('stream_checking', {}).get('enabled', False):
                    # Collect all channels for this profile that are in the active periods
                    for entry in active_periods.values():
                        if entry.get('profile_id') == p_id:
                            check_all_streams = profile.get('stream_checking', {}).get('check_all_streams', False)
                            for ch in entry['channels']:
                                ch_id = ch.get('id')
                                if ch_id is None:
                                    continue
                                channels_to_quality_check.append(ch_id)
                                if check_all_streams:
                                    channel_check_all_streams[ch_id] = True
                                elif ch_id not in channel_check_all_streams:
                                    channel_check_all_streams[ch_id] = False

                m3u_config = profile.get('m3u_update', {})
                if m3u_config.get('enabled', False):
                    pf_playlists = m3u_config.get('playlists', [])
                    if not pf_playlists:
                        update_all_playlists = True
                    else:
                        playlists_to_update.update(pf_playlists)

            playlists_refreshed = bool(update_all_playlists or playlists_to_update)
            self._update_run_status(
                counts={
                    "playlists_to_refresh": "all" if update_all_playlists else len(playlists_to_update),
                    "quality_check_candidates": len(set(channels_to_quality_check)),
                },
                stage="m3u_refresh" if playlists_refreshed else "stream_matching",
                stage_label="Refreshing M3U" if playlists_refreshed else "Matching Streams",
                message=(
                    "Refreshing configured playlists"
                    if playlists_refreshed
                    else "No playlist refresh requested; using current cache"
                ),
            )
            
            # 3. Update Playlists
            refresh_success = False
            refreshed_accounts = []
            refresh_degraded = False
            failed_refresh_requests = []
            provider_refresh_failed_count = 0
            pre_refresh_stream_count = 0
            post_refresh_stream_count = 0
            check_results = {}
            channel_visibility_events = []

            start_time = datetime.now()
            m3u_refresh_started = time.time()

            # Determine whether a provider playlist refresh will occur this cycle.
            # Pre/post stream counts and the safety gate are only meaningful when
            # playlists are actually refreshed — skip all three when they are not.
            if playlists_refreshed:
                try:
                    pre_refresh_stream_count = len(get_streams(log_result=False) or [])
                except Exception as e:
                    logger.warning(f"Could not read pre-refresh stream pool size: {e}")
                    pre_refresh_stream_count = 0

                # Capture stream list before provider fetch for changelog delta.
                # Must be done here (in run_automation_cycle) not inside refresh_playlists()
                # because refresh_playlists() no longer owns the changelog write.
                changelog_tracking_pre = self.config.get("enabled_features", {}).get("changelog_tracking", True)
                try:
                    streams_before = get_streams(log_result=False) if changelog_tracking_pre else []
                    before_stream_ids = {
                        s.get('id'): s.get('name', '')
                        for s in streams_before
                        if isinstance(s, dict) and s.get('id')
                    }
                except Exception as _sb_err:
                    logger.warning(f"Could not capture pre-refresh stream list: {_sb_err}")
                    before_stream_ids = {}
            else:
                before_stream_ids = {}

            def update_m3u_refresh_progress(
                payload: Dict[str, Any],
                *,
                current_override: Optional[int] = None,
                total_override: Optional[int] = None,
                message_override: Optional[str] = None,
            ) -> None:
                total = total_override if total_override is not None else payload.get("total")
                current = current_override if current_override is not None else payload.get("current", 0)
                message = message_override or payload.get("message") or "Refreshing configured playlists"
                counts = {
                    "m3u_refresh_current": current,
                    "m3u_refresh_total": total,
                    "m3u_refresh_state": payload.get("state", "running"),
                }
                for source_key, count_key in (
                    ("wait_elapsed_seconds", "m3u_refresh_wait_elapsed_seconds"),
                    ("wait_stable_polls", "m3u_refresh_wait_stable_polls"),
                    ("wait_busy_accounts", "m3u_refresh_wait_busy_accounts"),
                    ("wait_streams_seen", "m3u_refresh_wait_streams_seen"),
                    ("wait_failed_accounts", "m3u_refresh_wait_failed_accounts"),
                    ("wait_retry_count", "m3u_refresh_wait_retry_count"),
                ):
                    if source_key in payload:
                        counts[count_key] = payload.get(source_key)
                self._update_run_status(
                    stage="m3u_refresh",
                    stage_label="Refreshing M3U",
                    message=message,
                    counts=counts,
                    durations={"m3u_refresh_seconds": time.time() - m3u_refresh_started},
                    progress={
                        "current": current,
                        "total": total,
                        "message": message,
                    },
                )

            if update_all_playlists:
                logger.info("Updating ALL playlists (requested by one or more profiles)")
                refresh_result = self.refresh_playlists(
                    account_id=None,
                    skip_changelog=True,
                    progress_callback=update_m3u_refresh_progress,
                )
                refresh_success, refreshed_accounts = refresh_result
                failed_refresh_requests.extend(getattr(refresh_result, "failed_refresh_requests", []))
                provider_refresh_failed_count = max(
                    provider_refresh_failed_count,
                    len(failed_refresh_requests),
                )
                refresh_degraded = refresh_degraded or bool(getattr(refresh_result, "degraded", False))
            elif playlists_to_update:
                logger.info(f"Updating {len(playlists_to_update)} specific playlists: {playlists_to_update}")
                refresh_success = True
                ordered_playlist_ids = list(playlists_to_update)
                total_specific_playlists = len(ordered_playlist_ids)
                specific_refresh_failures = 0
                for refresh_index, acc_id in enumerate(ordered_playlist_ids, start=1):
                    def specific_refresh_progress(payload: Dict[str, Any], *, _index: int = refresh_index, _acc_id: Any = acc_id) -> None:
                        state = payload.get("state", "running")
                        account_name = payload.get("account_name") or f"Account {_acc_id}"
                        if state == "skipped":
                            current_value = _index
                            message = f"Playlist {_index}/{total_specific_playlists} skipped: {account_name}"
                        elif state in {"accepted", "completed", "failed"}:
                            current_value = _index
                            if state == "accepted":
                                verb = "accepted"
                            elif state == "completed":
                                verb = "refreshed"
                            else:
                                verb = "failed"
                            message = f"Playlist {_index}/{total_specific_playlists} {verb}: {account_name}"
                        elif state == "requesting":
                            current_value = _index - 1
                            message = f"Refreshing playlist {_index}/{total_specific_playlists}: {account_name}"
                        else:
                            current_value = _index - 1
                            message = f"Preparing playlist {_index}/{total_specific_playlists}: {account_name}"

                        update_m3u_refresh_progress(
                            payload,
                            current_override=current_value,
                            total_override=total_specific_playlists,
                            message_override=message,
                        )

                    refresh_result = self.refresh_playlists(
                        account_id=int(acc_id),
                        skip_changelog=True,
                        progress_callback=specific_refresh_progress,
                    )
                    success, accs = refresh_result
                    result_failures = list(getattr(refresh_result, "failed_refresh_requests", []))
                    if result_failures:
                        failed_refresh_requests.extend(result_failures)
                    if not success:
                        specific_refresh_failures += 1
                        if not result_failures:
                            failed_refresh_requests.append({"id": acc_id})
                    provider_refresh_failed_count = max(
                        provider_refresh_failed_count,
                        len(failed_refresh_requests),
                    )
                    refresh_degraded = refresh_degraded or bool(getattr(refresh_result, "degraded", False))
                    if accs:
                        refreshed_accounts.extend(accs)
                if specific_refresh_failures and refreshed_accounts:
                    refresh_success = True
                    refresh_degraded = True
                    update_m3u_refresh_progress(
                        {
                            "state": "partial",
                            "current": len(refreshed_accounts),
                            "total": len(refreshed_accounts) + specific_refresh_failures,
                            "message": (
                                f"Playlist refresh partially accepted: {len(refreshed_accounts)} accepted, "
                                f"{specific_refresh_failures} failed"
                            ),
                        }
                    )
                elif specific_refresh_failures:
                    refresh_success = False
            else:
                logger.info(
                    "No playlists to update based on active profile settings. "
                    "Cycle will operate on current UDI cache."
                )
                self.last_playlist_update = datetime.now()
                refresh_success = True

            self._update_run_status(
                counts={
                    "refreshed_playlists": len(refreshed_accounts),
                    "pre_refresh_streams": pre_refresh_stream_count,
                    "failed_refresh_requests": len(failed_refresh_requests),
                    "provider_refresh_failed_count": provider_refresh_failed_count,
                    "provider_refresh_degraded": refresh_degraded,
                },
                durations={"m3u_refresh_seconds": time.time() - m3u_refresh_started},
                message=(
                    "Playlist refresh requests partially accepted"
                    if refresh_success and refresh_degraded
                    else f"Playlist refresh requests {'accepted' if refresh_success else 'failed'}"
                    if playlists_refreshed
                    else "Current cache selected for stream matching"
                ),
            )
            if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                return

            validation_details = []
            assignment_details = []
            cycle_abort_message = None
            cycle_failed_message = None

            if playlists_refreshed and refresh_success:
                wait_result = self._wait_for_m3u_refresh_completion(
                    refreshed_accounts,
                    progress_callback=update_m3u_refresh_progress,
                )
                self._update_run_status(
                    counts={
                        "m3u_refresh_wait_state": wait_result.get("state"),
                        "m3u_refresh_wait_ok": bool(wait_result.get("ok")),
                    },
                    durations={"m3u_refresh_seconds": time.time() - m3u_refresh_started},
                    message=wait_result.get("message", "Playlist refresh wait completed"),
                )
                if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                    return
                if not wait_result.get("ok"):
                    cycle_abort_message = wait_result.get("message") or "Playlist refresh did not settle"
                    logger.error(cycle_abort_message)
                    refresh_success = False
                elif wait_result.get("state") == "partial":
                    refresh_degraded = True
                    wait_snapshot = wait_result.get("snapshot") or {}
                    failed_wait_count = int(wait_snapshot.get("failed_count") or 0)
                    provider_refresh_failed_count = max(
                        provider_refresh_failed_count,
                        failed_wait_count,
                    )
                    if failed_wait_count and not failed_refresh_requests:
                        failed_refresh_requests.extend(
                            {"id": account.get("id")}
                            for account in wait_snapshot.get("failed_accounts") or []
                            if isinstance(account, dict)
                        )

            # Deduplicate while preserving order (channels may appear in multiple active period groups).
            channels_to_quality_check = list(dict.fromkeys(channels_to_quality_check))

            # When playlists were refreshed, sync the UDI cache from Dispatcharr's
            # now-updated stream pool before running validation, assignment, or the
            # safety gate. Without this sync:
            #   - matching reads stale stream IDs → Invalid pk errors on write
            #   - changelog delta is always zero (streams_after == streams_before)
            #   - dead stream cleanup uses wrong URLs
            #   - safety gate compares two reads of the same stale cache
            #
            # When no playlists were refreshed (m3u_update=False across all active
            # profiles), the existing cache is used as-is. The background UDI sync
            # in the finally block handles cache accuracy for the next cycle.
            if playlists_refreshed and refresh_success:
                udi_sync_started = time.time()
                self._update_run_status(
                    stage="cache_sync",
                    stage_label="Syncing Cache",
                    message="Refreshing cache after playlist update",
                )
                logger.info(
                    "Syncing UDI cache after provider refresh — "
                    "matching and safety gate will use current stream IDs..."
                )
                sync_ok = False
                try:
                    _sync_udi = get_udi_manager()
                    sync_ok = self._sync_udi_cache_after_playlist_refresh(_sync_udi)
                    if sync_ok:
                        logger.info("UDI cache synced after provider refresh")
                    else:
                        logger.warning(
                            "UDI cache sync after provider refresh reported warnings - "
                            "proceeding with available cache"
                        )
                except FetchCancelled:
                    if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                        return
                    raise
                except Exception as _sync_err:
                    logger.warning(
                        f"UDI sync after provider refresh failed: {_sync_err} — "
                        "proceeding with potentially stale cache"
                    )

                # Capture streams_after from the now-current cache for changelog and cleanup
                changelog_tracking = self.config.get("enabled_features", {}).get("changelog_tracking", True)
                try:
                    streams_after = get_streams(log_result=True) if changelog_tracking else []
                    after_stream_ids = {
                        s.get('id'): s.get('name', '')
                        for s in streams_after
                        if isinstance(s, dict) and s.get('id')
                    }
                except Exception as _sa_err:
                    logger.warning(f"Could not capture post-refresh stream list: {_sa_err}")
                    streams_after = []
                    after_stream_ids = {}

                # Changelog entry with accurate delta
                if changelog_tracking:
                    try:
                        added_stream_ids = set(after_stream_ids.keys()) - set(before_stream_ids.keys())
                        removed_stream_ids = set(before_stream_ids.keys()) - set(after_stream_ids.keys())
                        added_streams = [{"id": sid, "name": after_stream_ids[sid]} for sid in added_stream_ids]
                        removed_streams = [{"id": sid, "name": before_stream_ids.get(sid, '')} for sid in removed_stream_ids]
                        self.changelog.add_entry("playlist_refresh", {
                            "success": True,
                            "job_outcome": (
                                "completed_degraded"
                                if refresh_degraded
                                else "completed"
                            ),
                            "timestamp": self.last_playlist_update.isoformat(),
                            "total_streams": len(after_stream_ids),
                            "added_streams": added_streams[:50],
                            "removed_streams": removed_streams[:50],
                            "added_count": len(added_streams),
                            "removed_count": len(removed_streams),
                            "failed_refresh_requests": len(failed_refresh_requests),
                            "provider_refresh_failed_count": provider_refresh_failed_count,
                            "degraded_count": provider_refresh_failed_count if refresh_degraded else 0,
                        })
                        logger.info(
                            f"Playlist changelog: {len(added_streams)} added, "
                            f"{len(removed_streams)} removed"
                        )
                    except Exception as _cl_err:
                        logger.warning(f"Could not write playlist changelog entry: {_cl_err}")

                # Dead stream cleanup using accurate current URLs
                dead_streams_tracker = getattr(self, "dead_streams_tracker", None)
                if dead_streams_tracker and streams_after:
                    try:
                        current_stream_urls = {
                            s.get('url', '') for s in streams_after
                            if isinstance(s, dict) and s.get('url')
                        }
                        current_stream_urls.discard('')
                        cleaned_count = dead_streams_tracker.cleanup_removed_streams(current_stream_urls)
                        if cleaned_count > 0:
                            logger.info(
                                f"Dead streams cleanup: removed {cleaned_count} "
                                "stream(s) no longer in playlist"
                            )
                    except Exception as _ds_err:
                        logger.warning(f"Dead stream cleanup failed: {_ds_err}")

                # Safety gate — now reads the updated cache, making it meaningful
                try:
                    post_refresh_stream_count = len(get_streams(log_result=False) or [])
                except Exception as e:
                    logger.warning(f"Could not read post-refresh stream pool size: {e}")
                    post_refresh_stream_count = 0

                if refresh_success and self._should_abort_for_suspicious_stream_pool(
                    before_count=pre_refresh_stream_count,
                    after_count=post_refresh_stream_count,
                    playlists_refreshed=playlists_refreshed,
                ):
                    logger.error(
                        "Automation safety gate triggered. Skipping validation and assignment "
                        "to preserve existing channel streams."
                    )
                    cycle_abort_message = "Automation safety gate stopped matching after playlist refresh"
                    refresh_success = False

                self._update_run_status(
                    counts={
                        "post_refresh_streams": post_refresh_stream_count,
                        "cache_sync_state": "completed" if sync_ok else "warning",
                    },
                    durations={"udi_sync_seconds": time.time() - udi_sync_started},
                    message=(
                        "Cache sync completed after playlist refresh"
                        if refresh_success and sync_ok
                        else "Cache sync completed with warnings after playlist refresh"
                        if refresh_success
                        else "Safety gate stopped matching after playlist refresh"
                    ),
                    stage="cache_sync" if refresh_success else "aborted",
                    stage_label="Syncing Cache" if refresh_success else "Aborted",
                )
                if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                    return

            if refresh_success:
                if channels_to_quality_check:
                    try:
                        stream_checker_for_guard = get_stream_checker_service()
                        failed_connectivity = stream_checker_for_guard._require_quality_check_connectivity(
                            phase='automation_quality_preflight',
                            update_progress=False,
                        )
                        if failed_connectivity is not None:
                            logger.error(
                                "Automation quality-check connectivity guard failed. "
                                "Skipping validation, assignment, and quality checks to preserve channel streams: %s",
                                failed_connectivity.message,
                            )
                            cycle_abort_message = failed_connectivity.message
                            refresh_success = False
                    except Exception as guard_err:
                        logger.error(
                            "Automation quality-check connectivity guard could not prove connectivity. "
                            "Skipping validation, assignment, and quality checks: %s",
                            guard_err,
                        )
                        cycle_abort_message = str(guard_err)
                        refresh_success = False

            if refresh_success:
                # Optional post-refresh delay for environments where provider updates
                # are eventually consistent. Defaults to 0 to avoid fixed latency.
                post_refresh_delay = self._get_post_refresh_delay_seconds()
                if playlists_refreshed and post_refresh_delay > 0:
                    logger.info(
                        f"Waiting {post_refresh_delay:.2f}s after playlist refresh before stream matching"
                    )
                    if self._manual_stop_requested.wait(timeout=post_refresh_delay):
                        if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                            return

                # 4. Stream Matching (Validation & Assignment)
                # Group results by channel for easier joining later
                matching_started = time.time()
                self._update_run_status(
                    stage="stream_matching",
                    stage_label="Matching Streams",
                    message="Validating existing streams and assigning new matches",
                )
                
                # Validate existing streams
                try:
                    val_res = self.validate_and_remove_non_matching_streams(force=forced, forced_period_id=forced_period_id, skip_changelog=True)
                    validation_details = val_res.get("details", [])
                    if val_res.get("aborted") or self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                        return
                except Exception as e:
                    logger.error(f"✗ Failed to validate streams: {e}")
                if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                    return

                # Discover and assign new streams
                try:
                    assign_res = self.discover_and_assign_streams(force=forced, skip_check_trigger=True, forced_period_id=forced_period_id, skip_changelog=True)
                    assignment_details = assign_res.get("assignment_details", [])
                    assigned_stream_ids = assign_res.get("assigned_stream_ids", {})
                    channel_visibility_events.extend(assign_res.get("channel_visibility_events", []) or [])
                    if assign_res.get("aborted") or self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                        return
                except Exception as e:
                    logger.error(f"✗ Failed to assign streams: {e}")
                    assigned_stream_ids = {}

                if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                    return

                visibility_summary = self._summarize_channel_visibility_events(channel_visibility_events)
                self._update_run_status(
                    counts={
                        "validated_channels": len(validation_details),
                        "assigned_channels": len(assignment_details),
                        **visibility_summary,
                    },
                    durations={"stream_matching_seconds": time.time() - matching_started},
                    message="Stream matching completed",
                )

                # 4.5. Trigger Quality Checks for all channels in the period(s)
                if channels_to_quality_check:
                    if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                        return
                    quality_stage_started = time.time()
                    self._update_run_status(
                        stage="quality_queueing",
                        stage_label="Queueing Quality Checks",
                        message="Selecting channels for quality checks",
                    )
                    try:
                        stream_checker = get_stream_checker_service()

                        # Normalise assigned_stream_ids to integer keys. The dict returned by
                        # _discover_and_assign_streams_impl uses integer channel IDs (from
                        # defaultdict keyed by channel_id integers), but lookups with
                        # str(ch_id) never matched. Normalising here ensures the lookup
                        # never misses due to an int/str type mismatch.
                        _assigned_by_int = {int(k): v for k, v in assigned_stream_ids.items()}
                        logger.info(
                            f"Running synchronous quality checks for "
                            f"{len(channels_to_quality_check)} eligible channels "
                            f"({len(_assigned_by_int)} received new assignments this cycle)"
                        )

                        channels_to_check_sync = []
                        _target_stream_ids = {}

                        for ch_id in channels_to_quality_check:
                            if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                                return
                            # Normalise ch_id to int for all lookups. channels_to_quality_check
                            # may contain mixed int/str entries if populated from multiple sources.
                            _ch_id_int = int(ch_id)
                            check_all_streams = channel_check_all_streams.get(_ch_id_int, False)
                            logger.debug(
                                f"Quality check loop: ch_id={ch_id!r}({type(ch_id).__name__}) "
                                f"check_all={check_all_streams} "
                                f"assigned={_assigned_by_int.get(_ch_id_int, None) is not None}"
                            )
                            if check_all_streams:
                                # check_all_streams=True: always include, no stream targeting
                                channels_to_check_sync.append(_ch_id_int)
                            elif _assigned_by_int.get(_ch_id_int):
                                # check_all_streams=False with new assignments: include, targeted to
                                # newly assigned streams only. Channels with no new assignments are
                                # omitted entirely — they fall through to the background worker's
                                # normal incremental logic rather than entering a full re-check via
                                # the grace_period=False branch in _check_channel_concurrent/sequential.
                                channels_to_check_sync.append(_ch_id_int)
                                _target_stream_ids[_ch_id_int] = _assigned_by_int[_ch_id_int]
                            # else: check_all_streams=False, no new assignments → skip this cycle

                        logger.info(
                            f"Quality check dispatch: {len(channels_to_check_sync)} channels selected "
                            f"({len(_target_stream_ids)} targeted, "
                            f"{len(channels_to_check_sync) - len(_target_stream_ids)} full-check, "
                            f"{len(channels_to_quality_check) - len(channels_to_check_sync)} skipped)"
                        )
                        if channels_to_check_sync:
                            from apps.stream.queue_start import order_channels_for_queue_start

                            start_mode = stream_checker.config.get('queue.start_mode', 'first')
                            start_channel_id = stream_checker.config.get('queue.start_channel_id', None)
                            channel_refs = [
                                channels_by_id.get(ch_id, {'id': ch_id})
                                for ch_id in channels_to_check_sync
                            ]
                            try:
                                ordered_refs, start_meta = order_channels_for_queue_start(
                                    channel_refs,
                                    start_mode=start_mode,
                                    start_channel_id=start_channel_id,
                                )
                            except ValueError as exc:
                                logger.warning(
                                    "Invalid saved quality-check start selection (%s); falling back to first channel",
                                    exc,
                                )
                                ordered_refs, start_meta = order_channels_for_queue_start(
                                    channel_refs,
                                    start_mode='first',
                                )
                            channels_to_check_sync = [int(channel['id']) for channel in ordered_refs]
                            logger.info(
                                "Synchronous quality checks start at %s (mode=%s)",
                                start_meta.get('start_channel_name', start_meta.get('start_channel_id')),
                                start_meta.get('mode', 'first'),
                            )
                        self._update_run_status(
                            counts={
                                "quality_candidates": len(channels_to_quality_check),
                                "quality_selected": len(channels_to_check_sync),
                                "quality_targeted": len(_target_stream_ids),
                                "quality_skipped": len(channels_to_quality_check) - len(channels_to_check_sync),
                            },
                            message=f"{len(channels_to_check_sync)} channel(s) selected for quality checks",
                        )

                        # Run checks synchronously and collect results
                        if channels_to_check_sync:
                            def _quality_progress_callback(completed_count, total_count, channel_result):
                                if self._is_manual_stop_requested():
                                    try:
                                        stream_checker.abort_current_check.set()
                                    except Exception:
                                        pass
                                channel_name = ""
                                if isinstance(channel_result, dict):
                                    channel_name = channel_result.get("channel_name") or ""
                                message = f"Checked {completed_count}/{total_count} channel(s)"
                                if channel_name:
                                    message = f"{message}: {channel_name}"
                                self._update_run_progress(
                                    stage_key="quality_checking",
                                    current=completed_count,
                                    total=total_count,
                                    message=message,
                                )

                            self._update_run_status(
                                stage="quality_checking",
                                stage_label="Quality Checking",
                                message="Running synchronous quality checks",
                                progress={
                                    "current": 0,
                                    "total": len(channels_to_check_sync),
                                    "message": f"Checking {len(channels_to_check_sync)} selected channel(s)",
                                },
                            )
                            target_stream_ids = _target_stream_ids if _target_stream_ids else None
                            if self._is_manual_stop_requested():
                                stream_checker.abort_current_check.set()
                            check_results = stream_checker.check_channels_synchronously(
                                channel_ids=channels_to_check_sync,
                                force_check=forced,
                                target_stream_ids=target_stream_ids,
                                progress_callback=_quality_progress_callback,
                                run_mode="automation_quality_check",
                            )
                            if self._abort_run_if_manual_stop_requested(active_periods=active_periods):
                                return
                            self._update_run_progress(
                                stage_key="quality_checking",
                                current=len(check_results),
                                total=len(channels_to_check_sync),
                                message="Quality checks completed",
                            )
                            logger.info(f"Synchronous quality checks completed for {len(check_results)} channels")
                        else:
                            logger.info("No channels require synchronous quality checks this cycle (no new assignments)")
                            check_results = {}
                            self._update_run_progress(
                                stage_key="quality_checking",
                                current=0,
                                total=0,
                                message="No channels required quality checks",
                            )
                        quality_summary = self._summarize_quality_check_results(
                            check_results,
                            expected_count=len(channels_to_check_sync),
                        )
                        if not quality_summary["ok"]:
                            cycle_abort_message = (
                                quality_summary["abort_message"]
                                or (
                                    "Quality check stage stopped before completion "
                                    f"({quality_summary['checked_count']}/"
                                    f"{quality_summary['expected_count']} channels checked)"
                                )
                            )
                            logger.error("Automation quality-check stage aborted: %s", cycle_abort_message)
                        self._update_run_status(
                            counts={
                                "quality_checked": quality_summary["checked_count"],
                                "quality_aborted": quality_summary["aborted_count"],
                                "quality_failed": quality_summary["failed_count"],
                                "quality_incomplete": quality_summary["incomplete_count"],
                            },
                            durations={"quality_check_seconds": time.time() - quality_stage_started},
                            message=(
                                "Quality check stage aborted"
                                if cycle_abort_message
                                else "Quality check stage completed"
                            ),
                            error=cycle_abort_message,
                        )
                    except Exception as e:
                        logger.error(f"✗ Failed to run quality checks: {e}")
                        cycle_failed_message = f"Quality check stage failed: {e}"
                        check_results = {}
                        self._update_run_status(
                            durations={"quality_check_seconds": time.time() - quality_stage_started},
                            error=cycle_failed_message,
                            message="Quality check stage failed",
                        )
            
            # 5. Consolidate Results by Period for Changelog
            self._update_run_status(
                stage="finalizing",
                stage_label="Finalizing",
                message="Summarizing automation results",
            )
            end_time = datetime.now()
            duration_sec = (end_time - start_time).total_seconds()
            duration_str = f"{int(duration_sec)}s"
            run_snapshot = copy.deepcopy((self.get_run_status() or {}).get("run_snapshot") or {})

            run_results = {
                'duration': duration_str,
                'total_channels': channels_with_periods,
                'run_snapshot': run_snapshot,
                'periods': [],
                'total_streams': 0,
                'streams_analyzed': 0,
                'good_streams': 0,
                'dead_streams': 0,
                'blank_streams': 0,
                'freeze_streams': 0,
                'streams_revived': 0,
                'added_streams': 0,
                'removed_streams': 0,
                'avg_bitrate': 'N/A',
                'avg_resolution': 'N/A',
                'avg_fps': 'N/A'
            }
            
            # Aggregate stats counters
            agg_bitrates = []
            agg_fps = []
            agg_resolutions = []
            total_streams_count = 0
            streams_analyzed_count = 0
            good_streams_count = 0
            dead_streams_count = 0
            blank_streams_count = 0
            freeze_streams_count = 0
            revived_streams_count = 0
            added_streams_count = 0
            removed_streams_count = 0
            quality_visibility_events = []
            
            # Map channel IDs to their results
            val_map = {str(d['channel_id']): d for d in validation_details}
            assign_map = {str(d['channel_id']): d for d in assignment_details}
            
            for (p_id, p_name), p_data in active_periods.items():
                period_entry = {
                    'period_id': p_id,
                    'period_name': p_name,
                    'channels': []
                }
                
                for channel in p_data['channels']:
                    c_id = str(channel.get('id'))
                    c_name = channel.get('name', f'Channel {c_id}')
                    
                    # Fetch logo URL
                    logo_url = None
                    logo_id = channel.get('logo_id')
                    if logo_id:
                        logo_url = f"/api/channels/logos/{logo_id}/cache"
                    
                    steps = []
                    
                    # Step 1: Playlist Refresh (if relevant for this period's profile)
                    profile_id = p_data['profile_id']
                    profile = automation_config.get_profile(profile_id) if profile_id else None
                    m3u_enabled = profile.get('m3u_update', {}).get('enabled', False) if profile else False
                    
                    if m3u_enabled:
                        steps.append({
                            'step': 'Playlist Refresh',
                            'status': (
                                'warning'
                                if refresh_success and refresh_degraded
                                else 'success'
                                if refresh_success
                                else ('skipped' if not refreshed_accounts else 'failed')
                            ),
                            'details': {
                                'accounts': refreshed_accounts,
                                'failed_refresh_requests': len(failed_refresh_requests),
                                'provider_refresh_failed_count': provider_refresh_failed_count,
                                'degraded': refresh_degraded,
                            }
                        })
                    
                    if c_id in val_map:
                        v_detail = val_map[c_id]
                        rem_count = v_detail.get('removed_count', 0)
                        removed_streams_count += rem_count
                        steps.append({
                            'step': 'Validation',
                            'status': 'success',
                            'details': {
                                'removed_count': rem_count,
                                'streams': v_detail.get('removed_streams', [])
                            }
                        })
                    
                    if c_id in assign_map:
                        a_detail = assign_map[c_id]
                        add_count = a_detail.get('stream_count', 0)
                        added_streams_count += add_count
                        steps.append({
                            'step': 'Assignment',
                            'status': 'success',
                            'details': {
                                'added_count': add_count,
                                'streams': a_detail.get('streams', [])
                            }
                        })
                    
                    if int(c_id) in check_results:
                        c_result = check_results[int(c_id)]
                        ch_dead = c_result.get('dead_streams_count', 0)
                        ch_revived = c_result.get('revived_streams_count', 0)
                        ch_analyzed = len(c_result.get('checked_streams', []))
                        checked_streams = c_result.get('checked_streams', [])
                        visibility_result = (
                            c_result.get('channel_visibility')
                            if isinstance(c_result.get('channel_visibility'), dict)
                            else None
                        )
                        if isinstance(c_result.get('channel_visibility'), dict):
                            quality_visibility_events.append(visibility_result)
                        ch_good = max(
                            int(c_result.get('good_streams_count', 0) or 0),
                            sum(
                                1 for stream in checked_streams
                                if stream.get('status') in {'completed', 'incomplete_bitrate'}
                                and stream.get('blank_detected') is not True
                                and stream.get('freeze_detected') is not True
                                and stream.get('dead_reason') not in {'blank', 'freeze', 'low_quality', 'offline', 'unstable'}
                                and (
                                    stream.get('status') == 'incomplete_bitrate'
                                    or stream.get('quality_reason_detail') in {None, '', 'none'}
                                )
                            ),
                        )
                        ch_blank = max(
                            int(c_result.get('blank_streams_count', 0) or 0),
                            sum(
                                1 for stream in checked_streams
                                if stream.get('blank_detected') is True or stream.get('status') == 'blank'
                            ),
                        )
                        ch_freeze = max(
                            int(c_result.get('freeze_streams_count', 0) or 0),
                            sum(
                                1 for stream in checked_streams
                                if stream.get('freeze_detected') is True or stream.get('status') == 'freeze'
                            ),
                        )
                        
                        good_streams_count += ch_good
                        dead_streams_count += ch_dead
                        blank_streams_count += ch_blank
                        freeze_streams_count += ch_freeze
                        revived_streams_count += ch_revived
                        streams_analyzed_count += ch_analyzed
                        
                        # Collect metrics for global averages
                        from apps.core.stream_stats_utils import parse_bitrate_value, parse_fps_value
                        for s in checked_streams:
                            br = parse_bitrate_value(s.get('bitrate'))
                            if br: agg_bitrates.append(br)
                            
                            f = parse_fps_value(s.get('fps'))
                            if f: agg_fps.append(f)
                            
                            res = s.get('resolution')
                            if res and res != 'N/A': agg_resolutions.append(res)

                        steps.append({
                            'step': 'Quality Check',
                            'status': 'success' if c_result.get('error') is None else 'failed',
                            'details': {
                                'good_streams_count': ch_good,
                                'dead_streams_count': ch_dead,
                                'blank_streams_count': ch_blank,
                                'freeze_streams_count': ch_freeze,
                                'revived_streams_count': ch_revived,
                                'skipped_streams_count': len(c_result.get('skipped_streams', [])),
                                'dead_streams': c_result.get('dead_streams', []),
                                'revived_streams': c_result.get('revived_streams', []),
                                'skipped_streams': c_result.get('skipped_streams', []),
                                'checked_streams': c_result.get('checked_streams', []),
                                'error': c_result.get('error')
                            }
                        })

                        if visibility_result and visibility_result.get('changed'):
                            visibility_action = visibility_result.get('action')
                            steps.append({
                                'step': 'Channel Visibility',
                                'status': 'warning' if visibility_action == 'hidden' else 'success',
                                'details': visibility_result,
                            })
                        
                        # Total streams count for this channel
                        total_streams_count += len(channel.get('streams', []))
                    
                    # Filter out channels with zero impact
                    # A channel has impact if:
                    # 1. Validation removed streams
                    # 2. Assignment added streams
                    # 3. Quality check found dead, revived, skipped, or actively checked streams
                    # 4. Playlist Refresh was explicitly triggered for this channel's accounts and was successful
                    has_impact = False
                    for step in steps:
                        if step['step'] == 'Validation' and step['details'].get('removed_count', 0) > 0:
                            has_impact = True
                        elif step['step'] == 'Assignment' and step['details'].get('added_count', 0) > 0:
                            has_impact = True
                        elif step['step'] == 'Quality Check':
                            d = step['details']
                            # Count streams that were actively checked (not from cache)
                            active_checks = 0
                            for cs in d.get('checked_streams', []):
                                if not cs.get('from_cache', False):
                                    active_checks += 1
                                    
                            if d.get('dead_streams_count', 0) > 0 or d.get('blank_streams_count', 0) > 0 \
                               or d.get('freeze_streams_count', 0) > 0 or d.get('revived_streams_count', 0) > 0 \
                               or active_checks > 0:
                                has_impact = True
                        elif step['step'] == 'Channel Visibility' and step['details'].get('changed'):
                            has_impact = True

                    if steps and has_impact:
                        period_entry['channels'].append({
                            'channel_id': int(c_id),
                            'channel_name': c_name,
                            'logo_url': logo_url,
                            'profile_id': profile_id,
                            'profile_name': p_data['profile_name'],
                            'steps': steps
                        })
                
                if period_entry['channels']:
                    run_results['periods'].append(period_entry)

            # Finalize aggregate stats
            all_visibility_events = list(channel_visibility_events) + quality_visibility_events
            visibility_summary = self._summarize_channel_visibility_events(all_visibility_events)
            run_results['total_streams'] = total_streams_count
            run_results['streams_analyzed'] = streams_analyzed_count
            run_results['good_streams'] = good_streams_count
            run_results['dead_streams'] = dead_streams_count
            run_results['blank_streams'] = blank_streams_count
            run_results['freeze_streams'] = freeze_streams_count
            run_results['streams_revived'] = revived_streams_count
            run_results['added_streams'] = added_streams_count
            run_results['removed_streams'] = removed_streams_count
            run_results['channels_hidden'] = visibility_summary['channels_hidden']
            run_results['channels_ready'] = visibility_summary['channels_ready']
            run_results['channel_visibility_changed'] = visibility_summary['channel_visibility_changed']
            if all_visibility_events:
                run_results['channel_visibility_events'] = [
                    event for event in all_visibility_events
                    if isinstance(event, dict) and event.get('changed')
                ]
            
            from apps.core.stream_stats_utils import format_bitrate, format_fps
            from collections import Counter
            
            if agg_bitrates:
                run_results['avg_bitrate'] = format_bitrate(sum(agg_bitrates) / len(agg_bitrates))
            if agg_fps:
                run_results['avg_fps'] = format_fps(sum(agg_fps) / len(agg_fps))
            if agg_resolutions:
                run_results['avg_resolution'] = Counter(agg_resolutions).most_common(1)[0][0]

            provider_refresh_outcome = (
                "completed_degraded"
                if refresh_success and refresh_degraded
                else "completed"
                if refresh_success and playlists_refreshed
                else "failed"
                if playlists_refreshed
                else "skipped"
            )
            run_job_outcome = (
                "aborted"
                if cycle_abort_message
                else "failed"
                if cycle_failed_message or not refresh_success
                else "completed_degraded"
                if refresh_degraded
                else "completed"
            )
            degraded_count = provider_refresh_failed_count if refresh_degraded else 0
            run_results['job_outcome'] = run_job_outcome
            run_results['provider_refresh_outcome'] = provider_refresh_outcome
            run_results['failed_refresh_requests'] = len(failed_refresh_requests)
            run_results['provider_refresh_failed_count'] = provider_refresh_failed_count
            run_results['degraded_count'] = degraded_count
            
            # Add to changelog if there's any work done
            has_work = any(len(p['channels']) > 0 for p in run_results['periods'])
            if has_work and self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_automation_run_entry(run_results)
            
            self._advance_period_run_timestamps(active_periods, run_job_outcome)

            self._update_run_status(
                counts={
                    "streams_analyzed": streams_analyzed_count,
                    "good_streams": good_streams_count,
                    "dead_streams": dead_streams_count,
                    "blank_streams": blank_streams_count,
                    "freeze_streams": freeze_streams_count,
                    "streams_revived": revived_streams_count,
                    "added_streams": added_streams_count,
                    "removed_streams": removed_streams_count,
                    "channels_hidden": visibility_summary["channels_hidden"],
                    "channels_ready": visibility_summary["channels_ready"],
                    "channel_visibility_changed": visibility_summary["channel_visibility_changed"],
                    "failed_refresh_requests": len(failed_refresh_requests),
                    "provider_refresh_failed_count": provider_refresh_failed_count,
                    "degraded_count": degraded_count,
                    "provider_refresh_degraded": refresh_degraded,
                },
                durations={"total_cycle_seconds": duration_sec},
            )
            cycle_outcome = self._finish_cycle_outcome(
                refresh_success=refresh_success,
                cycle_abort_message=cycle_abort_message,
                cycle_failed_message=cycle_failed_message,
                refresh_degraded=refresh_degraded,
            )
            
            if cycle_outcome == "completed":
                logger.info("Automation cycle completed")
            elif cycle_outcome == "completed_degraded":
                logger.warning("Automation cycle completed with provider refresh warnings")
            elif cycle_outcome == "aborted":
                logger.warning("Automation cycle aborted")
            else:
                logger.warning("Automation cycle failed")
            _cycle_did_work = True

        except Exception as exc:
            self._finish_run_status(
                state="failed",
                stage="failed",
                stage_label="Failed",
                message="Automation cycle failed",
                error=str(exc),
            )
            raise

        finally:
            self._m3u_accounts_cache = None
            automation_busy_guard.clear_automation_busy()
            self._manual_stop_requested.clear()

            # Background UDI sync — pull all writes from this cycle back into cache.
            # Only fires when the cycle actually completed matching/checking work.
            # Skipped on early returns (disabled, no active periods, safety gate abort)
            # and when UDI is not yet fully initialised (e.g. concurrent with startup).
            if locals().get('_cycle_did_work') and get_udi_manager().is_network_ready():
                _skip_stream_channel_sync = bool(
                    locals().get('playlists_refreshed') and locals().get('refresh_success')
                )

                def _background_cycle_udi_sync():
                    try:
                        _udi = get_udi_manager()
                        _udi.refresh_m3u_accounts()
                        if not _skip_stream_channel_sync:
                            _udi.refresh_streams()
                            _udi.refresh_channels()
                        _udi.refresh_channel_groups()
                        _udi.refresh_channel_profiles()
                        logger.debug("Background UDI sync completed after automation cycle")
                    except Exception as _e:
                        logger.warning(f"Background UDI sync failed after automation cycle: {_e}")

                threading.Thread(
                    target=_background_cycle_udi_sync,
                    daemon=True,
                    name="udi-sync-post-cycle",
                ).start()

    def _filter_channels_by_profile(self, channels: List[Dict], operation: str = "") -> List[Dict]:
        """
        Filter channels based on their effective profile settings.
        Deprecated: Logic moved into specific methods (validation/assignment) to handle per-channel profiles.
        This helper returns all channels to avoid breaking legacy callers if any.
        """
        return channels
        
    def trigger_automation(self, period_id=None, force=True):
        """Manually trigger an automation cycle.
        
        Args:
            period_id: Optional ID of a specific automation period to run
            force: If True (default), forces check bypassing grace periods. 
                   If False, respects grace periods (simulates scheduled run).
        """
        logger.info(f"Triggering manual automation cycle{' for period ' + period_id if period_id else ''} (force={force})")
        self.force_next_run = force
        self.forced_period_id = period_id
        if self.automation_thread and self.automation_thread.is_alive():
            self.automation_wake_event.set()
        else:
            # If not running, we could potentially run it synchronously or just log warning.
            # But the requirement is likely to trigger the *service*.
            logger.warning("Automation service not running, manual trigger queueing for next run or ignored")

    def request_active_run_stop(self) -> bool:
        """Request that the current automation run abort without stopping the scheduler."""
        self._ensure_run_status_fields()
        with self._run_status_lock:
            active_run = bool(
                self._run_status.get("active") or self._run_status.get("state") == "running"
            )
        if not active_run:
            return False

        self._persist_manual_stop_request()
        self._manual_stop_requested.set()
        self._update_run_status(
            message="Stop requested; active automation run is shutting down",
        )
        return True
            
    def start_automation(self):
        """Start the automation background thread."""
        if self.automation_thread and self.automation_thread.is_alive():
            logger.info("Automation service already running")
            return
            
        logger.info("Starting automation service...")
        self._ensure_run_status_fields()
        self._manual_stop_requested.clear()
        self.automation_running = True
        self.running = True
        self.automation_start_time = datetime.now()
        self.automation_wake_event.clear()
        self.automation_thread = threading.Thread(target=self._automation_loop, daemon=True)
        self.automation_thread.start()
        logger.info("Automation service started")
        
    def stop_automation(self):
        """Stop the automation background thread."""
        logger.info("Stopping automation service...")
        self._ensure_run_status_fields()
        with self._run_status_lock:
            active_run = bool(
                self._run_status.get("active") or self._run_status.get("state") == "running"
            )
        if active_run:
            self._manual_stop_requested.set()
            self._persist_manual_stop_request()
            self._update_run_status(
                message="Stop requested; automation is shutting down",
            )
        self.automation_running = False
        self.running = False
        self.automation_wake_event.set()  # Wake up thread to exit
        
        if self.automation_thread:
            self.automation_thread.join(timeout=5)
            logger.info("Automation service stopped")
        self.automation_start_time = None
            
    def _automation_loop(self):
        """Main loop for automation service."""
        logger.info("Automation background loop started")
        while self.automation_running:
            try:
                # Do not run automation cycles before the startup network UDI refresh
                # completes. is_initialized() alone is True from SQL storage load
                # (potentially empty — zero streams, zero M3U accounts).
                if not get_udi_manager().is_network_ready():
                    logger.debug(
                        "Automation loop waiting — UDI network refresh not yet complete"
                    )
                    self.automation_wake_event.wait(timeout=10)
                    self.automation_wake_event.clear()
                    continue

                # Run automation cycle
                # Pass forced period info to cycle
                forced = self.force_next_run
                period_id = self.forced_period_id
                
                # Reset forced flags before running
                self.force_next_run = False
                self.forced_period_id = None
                self.automation_wake_event.clear()
                
                self.run_automation_cycle(forced=forced, forced_period_id=period_id)
                
            except Exception as e:
                logger.error(f"Error in automation loop: {e}", exc_info=True)
            
            # Sleep interval (e.g. 60 seconds)
            # We use wait() on the event to allow early wake-up/exit
            if self.automation_running:
                # Loop interval can be configured, default to 60s for responsiveness to schedule changes
                self.automation_wake_event.wait(timeout=60)
                if self.automation_wake_event.is_set() and not self.automation_running:
                    break
                self.automation_wake_event.clear()
        
        logger.info("Automation background loop stopped")
