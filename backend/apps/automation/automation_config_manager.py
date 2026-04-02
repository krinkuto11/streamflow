#!/usr/bin/env python3
"""
Automation Configuration Manager

Manages Automation Profiles and Global Automation Settings.
Stores configuration in automation_config.json.
"""

import json
import os
import threading
import uuid
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from threading import RLock

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
AUTOMATION_CONFIG_FILE = CONFIG_DIR / 'automation_config.json'

class AutomationConfigManager:
    """
    Manages Automation Profiles and Settings.
    """
    
    def __init__(self):
        from apps.database.manager import get_db_manager
        self.db = get_db_manager()
        self._lock = RLock()
        logger.info("AutomationConfigManager initialized with SQL backend")
        
    def _create_default_profile(self):
        """Deprecated."""
        pass

    def _get_config_dict(self, key: str, default: Any = None) -> Any:
        return self.db.get_system_setting(key, default)

    def _set_config_dict(self, key: str, value: Any):
        return self.db.set_system_setting(key, value)
        
    def _load_config(self):
        """Deprecated."""
        pass

    def _save_config(self) -> bool:
        """Deprecated."""
        return True


    # --- Global Settings ---

    def get_global_settings(self) -> Dict[str, Any]:
        return {
            "regular_automation_enabled": self._get_config_dict("regular_automation_enabled", False),
            "playlist_update_interval_minutes": self._get_config_dict("playlist_update_interval_minutes", {"type": "interval", "value": 5}),
            "validate_existing_streams": self._get_config_dict("validate_existing_streams", False)
        }

    def update_global_settings(self, regular_automation_enabled: Optional[bool] = None, settings: Dict[str, Any] = None) -> bool:
        updates = settings or {}
        if isinstance(regular_automation_enabled, dict):
            updates.update(regular_automation_enabled)
        elif regular_automation_enabled is not None:
            updates["regular_automation_enabled"] = regular_automation_enabled
            
        if "regular_automation_enabled" in updates:
            self._set_config_dict("regular_automation_enabled", bool(updates["regular_automation_enabled"]))
        
        if "validate_existing_streams" in updates:
            self._set_config_dict("validate_existing_streams", bool(updates["validate_existing_streams"]))

        if "playlist_update_interval_minutes" in updates:
            new_val = updates["playlist_update_interval_minutes"]
            val_wrap = {"type": "interval", "value": new_val} if isinstance(new_val, int) else new_val
            self._set_config_dict("playlist_update_interval_minutes", val_wrap)

        return True

    def _profile_to_dict(self, p) -> dict:
        if not p: return None
        res = {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "enabled": p.enabled,
            "parallel_checks": p.parallel_checks
        }
        extra = self._normalize_extra_settings(p.extra_settings)
        if extra:
            res.update(extra)
        return res

    def _period_to_dict(self, per) -> dict:
        if not per: return None
        cron_val = per.cron_schedule or ""
        sched_type = "interval" if cron_val.isdigit() else "cron"
        res = {
            "id": str(per.id),
            "name": per.name,
            "cron_schedule": cron_val,
            "enabled": per.enabled,
            "channel_regex": per.channel_regex,
            "exclude_regex": per.exclude_regex,
            "matching_type": per.matching_type,
            "automation_type": per.automation_type,
            "schedule": {"type": sched_type, "value": cron_val}
        }
        extra = self._normalize_extra_settings(per.extra_settings)
        if extra:
            res.update(extra)
        return res

    def _normalize_extra_settings(self, extra_settings: Any) -> Dict[str, Any]:
        """Normalize persisted extra_settings to a dict for safe API serialization."""
        if not extra_settings:
            return {}
        if isinstance(extra_settings, dict):
            return extra_settings
        if isinstance(extra_settings, str):
            try:
                parsed = json.loads(extra_settings)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        logger.warning(
            "Ignoring non-dict extra_settings while serializing automation config: %s",
            type(extra_settings).__name__,
        )
        return {}

    # --- Profile Management ---

    def get_all_profiles(
        self,
        search: str = '',
        page: Optional[int] = None,
        per_page: int = 50,
    ) -> Any:
        """Return automation profiles.

        When *page* is None returns a plain list (backward compatible).
        When *page* is provided returns a pagination envelope dict.
        """
        from apps.database.models import AutomationProfile
        from apps.database.connection import get_session
        from sqlalchemy import asc as _asc
        session = get_session()
        try:
            q = session.query(AutomationProfile).order_by(_asc(AutomationProfile.name))
            if search:
                q = q.filter(AutomationProfile.name.ilike(f'%{search}%'))
            if page is None:
                return [self._profile_to_dict(p) for p in q.all()]
            total = q.count()
            offset = (page - 1) * per_page
            items = q.offset(offset).limit(per_page).all()
            total_pages = max(1, (total + per_page - 1) // per_page)
            return {
                'items': [self._profile_to_dict(p) for p in items],
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
                'has_next': (offset + per_page) < total,
                'has_prev': page > 1,
            }
        finally:
            session.close()

    def get_profile(self, profile_id: str) -> Optional[Dict]:
        from apps.database.models import AutomationProfile
        from apps.database.connection import get_session
        if not profile_id: return None
        try:
            pid = int(profile_id)
        except (TypeError, ValueError):
            return None
        session = get_session()
        try:
            p = session.query(AutomationProfile).filter(AutomationProfile.id == pid).first()
            return self._profile_to_dict(p)
        finally:
            session.close()

    def create_profile(self, profile_data: Dict) -> Optional[str]:
        from apps.database.models import AutomationProfile
        from apps.database.connection import get_session
        session = get_session()
        try:
            extra = {}
            for k,v in profile_data.items():
                if k not in ['name', 'description', 'enabled', 'parallel_checks']:
                    extra[k] = v
            p = AutomationProfile(
                name=profile_data.get("name", "New Profile"),
                description=profile_data.get("description", ""),
                enabled=profile_data.get("enabled", True),
                parallel_checks=profile_data.get("parallel_checks", 1),
                extra_settings=extra
            )
            session.add(p)
            session.commit()
            return str(p.id)
        except Exception as e:
            session.rollback()
            logger.error("Failed to create automation profile: %s", e)
            return None
        finally:
            session.close()

    def update_profile(self, profile_id: str, profile_data: Dict) -> bool:
        from apps.database.models import AutomationProfile
        from apps.database.connection import get_session
        try:
            pid = int(profile_id)
        except (TypeError, ValueError):
            return False
        session = get_session()
        try:
            p = session.query(AutomationProfile).filter(AutomationProfile.id == pid).first()
            if not p: return False
            if "name" in profile_data: p.name = profile_data["name"]
            if "description" in profile_data: p.description = profile_data["description"]
            if "enabled" in profile_data: p.enabled = profile_data["enabled"]
            current_extra = dict(p.extra_settings or {})
            for k,v in profile_data.items():
                if k not in ['name', 'description', 'enabled', 'parallel_checks', 'id']:
                    current_extra[k] = v
            p.extra_settings = current_extra
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error("Failed to update automation profile %s: %s", profile_id, e)
            return False
        finally:
            session.close()

    def _remove_profile_from_period_map(self, setting_key: str, profile_id_str: str) -> None:
        """Remove all period→profile entries that reference *profile_id_str* from a nested assignment map.

        The map structure is ``{entity_id: {period_id: profile_id}}``.  Entries whose inner dict becomes
        empty after removal are also pruned from the outer dict.
        """
        assignments = self._get_config_dict(setting_key, {})
        if not isinstance(assignments, dict):
            return
        changed = False
        for entity_id, period_map in list(assignments.items()):
            if not isinstance(period_map, dict):
                continue
            for period_id in list(period_map.keys()):
                if period_map[period_id] == profile_id_str:
                    del period_map[period_id]
                    changed = True
            if not period_map:
                del assignments[entity_id]
        if changed:
            self._set_config_dict(setting_key, assignments)

    def delete_profile(self, profile_id: str) -> bool:
        from apps.database.models import AutomationProfile
        from apps.database.connection import get_session
        try:
            pid = int(profile_id)
        except (TypeError, ValueError):
            return False
        session = get_session()
        try:
            p = session.query(AutomationProfile).filter(AutomationProfile.id == pid).first()
            if not p: return False
            session.delete(p)
            session.commit()
            pid_str = str(pid)

            # Remove from channel-level profile assignments
            channel_assignments = self._get_config_dict("channel_assignments", {})
            if isinstance(channel_assignments, dict):
                changed = False
                for cid in list(channel_assignments.keys()):
                    if channel_assignments[cid] == pid_str:
                        del channel_assignments[cid]
                        changed = True
                if changed:
                    self._set_config_dict("channel_assignments", channel_assignments)

            # Remove from group-level profile assignments
            group_assignments = self._get_config_dict("group_assignments", {})
            if isinstance(group_assignments, dict):
                changed = False
                for gid in list(group_assignments.keys()):
                    if group_assignments[gid] == pid_str:
                        del group_assignments[gid]
                        changed = True
                if changed:
                    self._set_config_dict("group_assignments", group_assignments)

            # Remove from channel-level EPG scheduled profile assignments
            channel_epg = self._get_config_dict("channel_epg_scheduled_assignments", {})
            if isinstance(channel_epg, dict):
                changed = False
                for cid in list(channel_epg.keys()):
                    if channel_epg[cid] == pid_str:
                        del channel_epg[cid]
                        changed = True
                if changed:
                    self._set_config_dict("channel_epg_scheduled_assignments", channel_epg)

            # Remove from group-level EPG scheduled profile assignments
            group_epg = self._get_config_dict("group_epg_scheduled_assignments", {})
            if isinstance(group_epg, dict):
                changed = False
                for gid in list(group_epg.keys()):
                    if group_epg[gid] == pid_str:
                        del group_epg[gid]
                        changed = True
                if changed:
                    self._set_config_dict("group_epg_scheduled_assignments", group_epg)

            # Remove deleted profile from period assignment maps (channel and group)
            self._remove_profile_from_period_map("channel_period_assignments", pid_str)
            self._remove_profile_from_period_map("group_period_assignments", pid_str)

            return True
        except Exception as e:
            session.rollback()
            logger.error("Failed to delete automation profile %s: %s", profile_id, e)
            return False
        finally:
            session.close()

    # --- Assignments ---

    def assign_profile_to_channel(self, channel_id: int, profile_id: Optional[str]) -> bool:
        assignments = self._get_config_dict("channel_assignments", {})
        cid = str(channel_id)
        if profile_id is None:
            if cid in assignments: del assignments[cid]
        else: assignments[cid] = str(profile_id)
        return self._set_config_dict("channel_assignments", assignments)

    def assign_profile_to_channels(self, channel_ids: List[int], profile_id: Optional[str]) -> bool:
        assignments = self._get_config_dict("channel_assignments", {})
        changed = False
        for cid_raw in channel_ids:
            cid = str(cid_raw)
            if profile_id is None:
                if cid in assignments: del assignments[cid]; changed = True
            else:
                if assignments.get(cid) != str(profile_id): assignments[cid] = str(profile_id); changed = True
        if changed: return self._set_config_dict("channel_assignments", assignments)
        return True

    def assign_profile_to_group(self, group_id: int, profile_id: Optional[str]) -> bool:
        assignments = self._get_config_dict("group_assignments", {})
        gid = str(group_id)
        if profile_id is None:
            if gid in assignments: del assignments[gid]
        else: assignments[gid] = str(profile_id)
        return self._set_config_dict("group_assignments", assignments)
            
    def get_channel_assignment(self, channel_id: int) -> Optional[str]:
        return self._get_config_dict("channel_assignments", {}).get(str(channel_id))

    def get_group_assignment(self, group_id: int) -> Optional[str]:
        return self._get_config_dict("group_assignments", {}).get(str(group_id))

    def get_all_group_assignments(self) -> Dict[str, str]:
        """Return all group→automation-profile assignments as {group_id_str: profile_id_str}."""
        result = self._get_config_dict("group_assignments", {})
        return result if isinstance(result, dict) else {}

    def get_effective_profile_id(self, channel_id: int, group_id: Optional[int] = None) -> Optional[str]:
        cid = str(channel_id)
        chan = self._get_config_dict("channel_assignments", {})
        if cid in chan: return chan[cid]
        if group_id is not None:
             grp = self._get_config_dict("group_assignments", {})
             if str(group_id) in grp: return grp[str(group_id)]
        return None

    def get_effective_profile(self, channel_id: int, group_id: Optional[int] = None) -> Optional[Dict]:
        pid = self.get_effective_profile_id(channel_id, group_id)
        return self.get_profile(pid) if pid else None

    # --- EPG Scheduled Profile Assignments ---

    def assign_epg_scheduled_profile_to_channel(self, channel_id: int, profile_id: Optional[str]) -> bool:
        assignments = self._get_config_dict("channel_epg_scheduled_assignments", {})
        cid = str(channel_id)
        if profile_id is None:
            if cid in assignments:
                del assignments[cid]
        else:
            assignments[cid] = str(profile_id)
        return self._set_config_dict("channel_epg_scheduled_assignments", assignments)

    def assign_epg_scheduled_profile_to_channels(self, channel_ids: List[int], profile_id: Optional[str]) -> bool:
        assignments = self._get_config_dict("channel_epg_scheduled_assignments", {})
        changed = False
        for cid_raw in channel_ids:
            cid = str(cid_raw)
            if profile_id is None:
                if cid in assignments:
                    del assignments[cid]
                    changed = True
            else:
                if assignments.get(cid) != str(profile_id):
                    assignments[cid] = str(profile_id)
                    changed = True
        if changed:
            return self._set_config_dict("channel_epg_scheduled_assignments", assignments)
        return True

    def assign_epg_scheduled_profile_to_group(self, group_id: int, profile_id: Optional[str]) -> bool:
        assignments = self._get_config_dict("group_epg_scheduled_assignments", {})
        gid = str(group_id)
        if profile_id is None:
            if gid in assignments:
                del assignments[gid]
        else:
            assignments[gid] = str(profile_id)
        return self._set_config_dict("group_epg_scheduled_assignments", assignments)

    def get_all_group_epg_scheduled_assignments(self) -> Dict[str, str]:
        """Return all group→EPG-profile assignments as {group_id_str: profile_id_str}."""
        result = self._get_config_dict("group_epg_scheduled_assignments", {})
        return result if isinstance(result, dict) else {}

    def get_channel_epg_scheduled_assignment(self, channel_id: int) -> Optional[str]:
        return self._get_config_dict("channel_epg_scheduled_assignments", {}).get(str(channel_id))

    def get_group_epg_scheduled_assignment(self, group_id: int) -> Optional[str]:
        return self._get_config_dict("group_epg_scheduled_assignments", {}).get(str(group_id))

    def get_effective_epg_scheduled_profile_id(self, channel_id: int, group_id: Optional[int] = None) -> Optional[str]:
        cid = str(channel_id)
        chan = self._get_config_dict("channel_epg_scheduled_assignments", {})
        if cid in chan: return chan[cid]
        if group_id is not None:
            grp = self._get_config_dict("group_epg_scheduled_assignments", {})
            if str(group_id) in grp: return grp[str(group_id)]
        return None

    def get_effective_epg_scheduled_profile(self, channel_id: int, group_id: Optional[int] = None) -> Optional[Dict]:
        pid = self.get_effective_epg_scheduled_profile_id(channel_id, group_id)
        return self.get_profile(pid) if pid else None

    # --- Automation Periods Management ---

    def get_all_periods(
        self,
        search: str = '',
        page: Optional[int] = None,
        per_page: int = 50,
    ) -> Any:
        """Return automation periods.

        When *page* is None returns a plain list (backward compatible).
        When *page* is provided returns a pagination envelope dict.
        """
        from apps.database.models import AutomationPeriod
        from apps.database.connection import get_session
        from sqlalchemy import asc as _asc
        session = get_session()
        try:
            q = session.query(AutomationPeriod).order_by(_asc(AutomationPeriod.name))
            if search:
                q = q.filter(AutomationPeriod.name.ilike(f'%{search}%'))
            if page is None:
                return [self._period_to_dict(p) for p in q.all()]
            total = q.count()
            offset = (page - 1) * per_page
            items = q.offset(offset).limit(per_page).all()
            total_pages = max(1, (total + per_page - 1) // per_page)
            return {
                'items': [self._period_to_dict(p) for p in items],
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
                'has_next': (offset + per_page) < total,
                'has_prev': page > 1,
            }
        finally:
            session.close()

    def get_period(self, period_id: str) -> Optional[Dict]:
        from apps.database.models import AutomationPeriod
        from apps.database.connection import get_session
        try:
            pid = int(period_id)
        except (TypeError, ValueError):
            return None
        session = get_session()
        try:
            return self._period_to_dict(session.query(AutomationPeriod).get(pid))
        finally:
            session.close()

    def create_period(self, period_data: Dict) -> Optional[str]:
        from apps.database.models import AutomationPeriod
        from apps.database.connection import get_session
        session = get_session()
        try:
            sched = period_data.get("schedule", {})
            cron = sched.get("value") if isinstance(sched, dict) else period_data.get("cron_schedule", "0 * * * *")
            p = AutomationPeriod(
                name=period_data.get("name", "New Period"),
                profile_id=int(period_data.get("profile_id", 1)),
                cron_schedule=cron,
                enabled=period_data.get("enabled", True),
                channel_regex=period_data.get("channel_regex"),
                exclude_regex=period_data.get("exclude_regex"),
                matching_type=period_data.get("matching_type"),
                automation_type=period_data.get("automation_type"),
                extra_settings=period_data.get("extra_settings", {})
            )
            session.add(p)
            session.commit()
            return str(p.id)
        except Exception as e:
            session.rollback()
            logger.error("Failed to create automation period: %s", e)
            return None
        finally:
            session.close()

    def update_period(self, period_id: str, period_data: Dict) -> bool:
        from apps.database.models import AutomationPeriod
        from apps.database.connection import get_session
        try:
            pid = int(period_id)
        except (TypeError, ValueError):
            return False
        session = get_session()
        try:
            p = session.query(AutomationPeriod).get(pid)
            if not p: return False
            if "name" in period_data: p.name = period_data["name"]
            if "enabled" in period_data: p.enabled = period_data["enabled"]
            if "profile_id" in period_data: p.profile_id = int(period_data["profile_id"])
            if "channel_regex" in period_data: p.channel_regex = period_data["channel_regex"]
            if "exclude_regex" in period_data: p.exclude_regex = period_data["exclude_regex"]
            if "matching_type" in period_data: p.matching_type = period_data["matching_type"]
            if "automation_type" in period_data: p.automation_type = period_data["automation_type"]
            if "extra_settings" in period_data: p.extra_settings = period_data["extra_settings"]
            
            # Map schedule dictionary back to cron_schedule column
            if "schedule" in period_data:
                sched = period_data["schedule"]
                if isinstance(sched, dict) and "value" in sched:
                    p.cron_schedule = str(sched["value"])
            elif "cron_schedule" in period_data:
                p.cron_schedule = str(period_data["cron_schedule"])

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error("Failed to update automation period %s: %s", period_id, e)
            return False
        finally:
            session.close()

    def delete_period(self, period_id: str) -> bool:
        from apps.database.models import AutomationPeriod
        from apps.database.connection import get_session
        try:
            pid = int(period_id)
        except (TypeError, ValueError):
            return False
        session = get_session()
        try:
            p = session.query(AutomationPeriod).get(pid)
            if not p: return False
            session.delete(p)
            session.commit()

            # Remove orphaned references to deleted period from assignment maps.
            pid_str = str(period_id)
            changed = False

            channel_assignments = self._get_config_dict("channel_period_assignments", {})
            if isinstance(channel_assignments, dict):
                for cid, period_map in list(channel_assignments.items()):
                    if not isinstance(period_map, dict):
                        continue
                    if pid_str in period_map:
                        del period_map[pid_str]
                        if not period_map:
                            del channel_assignments[cid]
                        changed = True
                if changed:
                    self._set_config_dict("channel_period_assignments", channel_assignments)

            group_assignments = self._get_config_dict("group_period_assignments", {})
            group_changed = False
            if isinstance(group_assignments, dict):
                for gid, period_map in list(group_assignments.items()):
                    if not isinstance(period_map, dict):
                        continue
                    if pid_str in period_map:
                        del period_map[pid_str]
                        if not period_map:
                            del group_assignments[gid]
                        group_changed = True
                if group_changed:
                    self._set_config_dict("group_period_assignments", group_assignments)

            return True
        except Exception as e:
            session.rollback()
            logger.error("Failed to delete automation period %s: %s", period_id, e)
            return False
        finally:
            session.close()

    def assign_period_to_channels(self, period_id: str, channel_ids: List[int], profile_id: str, replace: bool = False) -> bool:
        assignments = self._get_config_dict("channel_period_assignments", {})
        pid = str(period_id)
        changed = False

        for cid_raw in channel_ids:
            cid = str(cid_raw)
            if replace or cid not in assignments or not isinstance(assignments[cid], dict):
                assignments[cid] = {}
            if assignments[cid].get(pid) != str(profile_id):
                assignments[cid][pid] = str(profile_id)
                changed = True

        if changed:
            return self._set_config_dict("channel_period_assignments", assignments)
        return True

    def remove_period_from_channels(self, period_id: str, channel_ids: List[int]) -> bool:
        assignments = self._get_config_dict("channel_period_assignments", {})
        pid = str(period_id)
        changed = False

        for cid_raw in channel_ids:
            cid = str(cid_raw)
            channel_assignments = assignments.get(cid)
            if not isinstance(channel_assignments, dict):
                continue
            if pid in channel_assignments:
                del channel_assignments[pid]
                if not channel_assignments:
                    del assignments[cid]
                changed = True

        if changed:
            return self._set_config_dict("channel_period_assignments", assignments)
        return True

    def get_channel_periods(self, channel_id: int) -> Dict[str, str]:
        assignments = self._get_config_dict("channel_period_assignments", {})
        channel_assignments = assignments.get(str(channel_id), {})
        if not isinstance(channel_assignments, dict):
            return {}
        return channel_assignments

    def get_effective_channel_periods(self, channel_id: int, group_id: int = None) -> Dict[str, str]:
        """Return the combined period assignments for a channel, merging group-level and channel-level assignments.

        Group-level assignments are used as the base; channel-specific assignments override them.
        """
        effective: Dict[str, str] = {}
        if group_id is not None:
            effective.update(self.get_group_periods(group_id))
        effective.update(self.get_channel_periods(channel_id))
        return effective

    def get_effective_period_channel_profiles(self, period_id: str) -> Dict[int, str]:
        """Return effective channel -> profile assignments for a period.

        Group-level assignments are used as the base and channel-level assignments override them.
        """
        pid = str(period_id)
        effective_assignments: Dict[int, str] = {}

        # Base assignments from group-level period/profile mappings.
        group_assignments = self._get_config_dict("group_period_assignments", {})
        groups_with_period: Dict[int, str] = {}
        for gid_raw, period_map in group_assignments.items():
            if not isinstance(period_map, dict) or pid not in period_map:
                continue
            try:
                gid = int(gid_raw)
            except (TypeError, ValueError):
                continue
            profile_id = period_map.get(pid)
            if profile_id:
                groups_with_period[gid] = str(profile_id)

        valid_channel_ids: Optional[set] = None
        try:
            from apps.udi import get_udi_manager
            udi = get_udi_manager()
            udi_channels = udi.get_channels() or []
            # When UDI has no loaded channel inventory (for example in isolated
            # tests or before initialization), do not filter explicit assignments.
            if udi_channels:
                valid_channel_ids = {
                    int(ch.get('id'))
                    for ch in udi_channels
                    if isinstance(ch, dict) and ch.get('id') is not None
                }
            else:
                valid_channel_ids = None
        except Exception:
            valid_channel_ids = None

        if groups_with_period:
            try:
                for gid, profile_id in groups_with_period.items():
                    channels = udi.get_channels_by_group(gid) or []
                    for channel in channels:
                        channel_id_raw = channel.get('id')
                        try:
                            channel_id = int(channel_id_raw)
                        except (TypeError, ValueError):
                            continue
                        if valid_channel_ids is not None and channel_id not in valid_channel_ids:
                            continue
                        effective_assignments[channel_id] = profile_id
            except Exception as e:
                logger.warning(
                    "Failed to resolve group period assignments for period %s via UDI: %s",
                    pid,
                    e,
                )

        # Channel-level assignments override group-level assignments.
        channel_assignments = self._get_config_dict("channel_period_assignments", {})
        for cid_raw, period_map in channel_assignments.items():
            if not isinstance(period_map, dict) or pid not in period_map:
                continue
            try:
                channel_id = int(cid_raw)
            except (TypeError, ValueError):
                continue
            if valid_channel_ids is not None and channel_id not in valid_channel_ids:
                continue
            profile_id = period_map.get(pid)
            if profile_id:
                effective_assignments[channel_id] = str(profile_id)

        return effective_assignments

    def get_period_channels(self, period_id: str) -> List[int]:
        return sorted(self.get_effective_period_channel_profiles(period_id).keys())

    # --- Group Period Assignments ---

    def assign_period_to_groups(self, period_id: str, group_ids: List[int], profile_id: str, replace: bool = False) -> bool:
        """Assign an automation period with a profile to one or more groups."""
        assignments = self._get_config_dict("group_period_assignments", {})
        pid = str(period_id)
        changed = False

        for gid_raw in group_ids:
            gid = str(gid_raw)
            if replace or gid not in assignments or not isinstance(assignments[gid], dict):
                assignments[gid] = {}
            if assignments[gid].get(pid) != str(profile_id):
                assignments[gid][pid] = str(profile_id)
                changed = True

        if changed:
            return self._set_config_dict("group_period_assignments", assignments)
        return True

    def remove_period_from_groups(self, period_id: str, group_ids: List[int]) -> bool:
        """Remove an automation period from one or more groups."""
        assignments = self._get_config_dict("group_period_assignments", {})
        pid = str(period_id)
        changed = False

        for gid_raw in group_ids:
            gid = str(gid_raw)
            group_assignments = assignments.get(gid)
            if not isinstance(group_assignments, dict):
                continue
            if pid in group_assignments:
                del group_assignments[pid]
                if not group_assignments:
                    del assignments[gid]
                changed = True

        if changed:
            return self._set_config_dict("group_period_assignments", assignments)
        return True

    def get_group_periods(self, group_id: int) -> Dict[str, str]:
        """Return a mapping of {period_id: profile_id} for a group."""
        assignments = self._get_config_dict("group_period_assignments", {})
        group_assignments = assignments.get(str(group_id), {})
        if not isinstance(group_assignments, dict):
            return {}
        return group_assignments

    def get_period_groups(self, period_id: str) -> List[int]:
        """Return the list of group IDs that have this period assigned."""
        assignments = self._get_config_dict("group_period_assignments", {})
        pid = str(period_id)
        groups: List[int] = []
        for gid, period_map in assignments.items():
            if isinstance(period_map, dict) and pid in period_map:
                try:
                    groups.append(int(gid))
                except (TypeError, ValueError):
                    continue
        return groups

    # --- Outer scheduler helpers ---

    def is_period_active_now(self, period_id: str) -> bool: return True

    def get_active_periods_for_channel(self, channel_id: int, group_id: Optional[int] = None) -> List[Dict]:
        pid_profile = self.get_effective_channel_periods(channel_id, group_id)
        res = []
        for pid, profile_id in pid_profile.items():
            period = self.get_period(pid)
            if period:
                period_with_profile = period.copy()
                period_with_profile["profile"] = self.get_profile(profile_id)
                period_with_profile["profile_id"] = profile_id
                res.append(period_with_profile)
        return res

    def get_effective_configuration(self, channel_id: int, group_id: Optional[int] = None) -> Optional[Dict]:
        active_periods = self.get_active_periods_for_channel(channel_id, group_id)
        if active_periods:
            if len(active_periods) > 1: active_periods.sort(key=lambda p: (-int(p.get('priority', 0)), p.get('id', '')))
            period = active_periods[0]
            profile = period.get('profile')
            if profile:
                 return {'source': 'period', 'periods': active_periods, 'period_id': period.get('id'), 'period_name': period.get('name'), 'profile': profile}
        return None

# Singleton instance
_automation_config_manager = None
_manager_lock = threading.Lock()

def get_automation_config_manager() -> AutomationConfigManager:
    global _automation_config_manager
    if _automation_config_manager is None:
        with _manager_lock:
            if _automation_config_manager is None: _automation_config_manager = AutomationConfigManager()
    return _automation_config_manager
