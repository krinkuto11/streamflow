"""
Match Profiles Manager for StreamFlow.

This module manages match profiles that define how streams are matched to channels.
Supports:
- Three match types: Regex (stream name), TVG-ID (exact match), Regex (stream URL)
- Dynamic variables: {channel_name}, {channel_group}, {m3u_account_name}
- Profile application at channel or group level
- Visual pipeline/building blocks structure
"""

import re
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

from apps.core.logging_config import setup_logging
from apps.udi.models import MatchProfile, MatchProfileStep

logger = setup_logging(__name__)


class MatchProfilesManager:
    """Manager for match profiles that define stream-to-channel matching rules."""
    
    def __init__(self, storage: Optional[Any] = None):
        """
        Initialize the MatchProfilesManager using the database session directly.
        """
        from apps.database.connection import get_session
        self.get_session = get_session
        logger.info("MatchProfilesManager initialized with SQL backend")
    
    def _orm_to_model(self, orm_profile) -> MatchProfile:
        """Helper to convert ORM profile with steps to UDI `MatchProfile` object."""
        # Convert steps to dicts first
        steps_data = []
        for step in sorted(orm_profile.steps, key=lambda s: s.step_order):
            steps_data.append({
                'id': step.id,
                'type': step.type,
                'pattern': step.pattern,
                'variables': step.variables,
                'enabled': step.enabled,
                'order': step.step_order
            })
            
        profile_data = {
            'id': orm_profile.id,
            'name': orm_profile.name,
            'description': orm_profile.description,
            'steps': steps_data,
            'enabled': orm_profile.enabled,
            'created_at': orm_profile.created_at.isoformat() if orm_profile.created_at else None,
            'updated_at': orm_profile.updated_at.isoformat() if orm_profile.updated_at else None
        }
        return MatchProfile.from_dict(profile_data)

    def list_profiles(self) -> List[MatchProfile]:
        """
        Get all match profiles.
        """
        from apps.database.models import MatchProfile as DBMatchProfile
        from sqlalchemy.orm import joinedload
        
        session = self.get_session()
        try:
            # Eager load steps to avoid detached errors
            profiles = session.query(DBMatchProfile).options(joinedload(DBMatchProfile.steps)).all()
            return [self._orm_to_model(p) for p in profiles]
        finally:
            session.close()
    
    def get_profile(self, profile_id: int) -> Optional[MatchProfile]:
        """
        Get a specific match profile by ID.
        """
        from apps.database.models import MatchProfile as DBMatchProfile
        from sqlalchemy.orm import joinedload
        
        session = self.get_session()
        try:
            p = session.query(DBMatchProfile).options(joinedload(DBMatchProfile.steps)).filter(DBMatchProfile.id == profile_id).first()
            if p:
                return self._orm_to_model(p)
            return None
        finally:
            session.close()
    
    def create_profile(self, name: str, description: Optional[str] = None,
                       steps: Optional[List[Dict[str, Any]]] = None) -> MatchProfile:
        """
        Create a new match profile.
        """
        from apps.database.models import MatchProfile as DBMatchProfile, MatchProfileStep as DBMatchProfileStep
        
        session = self.get_session()
        try:
            profile = DBMatchProfile(
                name=name,
                description=description,
                enabled=True
            )
            session.add(profile)
            session.flush() # Populate profile.id
            
            if steps:
                for idx, step_item in enumerate(steps):
                    step = DBMatchProfileStep(
                        profile_id=profile.id,
                        type=step_item.get('type'),
                        pattern=step_item.get('pattern'),
                        variables=step_item.get('variables'),
                        enabled=step_item.get('enabled', True),
                        step_order=step_item.get('order', idx)
                    )
                    session.add(step)
            
            session.commit()
            logger.info(f"Created match profile: {name} (ID: {profile.id})")
            return self._orm_to_model(profile)
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create match profile {name}: {e}")
            raise
        finally:
            session.close()
    
    def update_profile(self, profile_id: int, name: Optional[str] = None,
                       description: Optional[str] = None, steps: Optional[List[Dict[str, Any]]] = None,
                       enabled: Optional[bool] = None) -> Optional[MatchProfile]:
        """
        Update an existing match profile.
        """
        from apps.database.models import MatchProfile as DBMatchProfile, MatchProfileStep as DBMatchProfileStep
        
        session = self.get_session()
        try:
            profile = session.query(DBMatchProfile).filter(DBMatchProfile.id == profile_id).first()
            if not profile:
                logger.warning(f"Match profile {profile_id} not found")
                return None
            
            if name is not None: profile.name = name
            if description is not None: profile.description = description
            if enabled is not None: profile.enabled = enabled
            
            if steps is not None:
                # Easiest way in SQLite that matches typical behavior: recreate steps list
                session.query(DBMatchProfileStep).filter(DBMatchProfileStep.profile_id == profile_id).delete()
                for idx, step_item in enumerate(steps):
                    step = DBMatchProfileStep(
                        profile_id=profile_id,
                        type=step_item.get('type'),
                        pattern=step_item.get('pattern'),
                        variables=step_item.get('variables'),
                        enabled=step_item.get('enabled', True),
                        step_order=step_item.get('order', idx)
                    )
                    session.add(step)
                    
            profile.updated_at = datetime.now()
            session.commit()
            logger.info(f"Updated match profile: {profile_id}")
            return self.get_profile(profile_id)
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to update match profile {profile_id}: {e}")
            return None
        finally:
            session.close()
    
    def delete_profile(self, profile_id: int) -> bool:
        """
        Delete a match profile.
        """
        from apps.database.models import MatchProfile as DBMatchProfile
        
        session = self.get_session()
        try:
            profile = session.query(DBMatchProfile).filter(DBMatchProfile.id == profile_id).first()
            if not profile:
                logger.warning(f"Match profile {profile_id} not found")
                return False
                
            session.delete(profile)
            session.commit()
            logger.info(f"Deleted match profile: {profile_id}")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to delete match profile: {profile_id}")
            return False
        finally:
            session.close()
    
    def apply_profile_to_variables(self, profile: MatchProfile, 
                                   channel_name: str = "",
                                   channel_group: str = "",
                                   m3u_account_name: str = "") -> MatchProfile:
        """
        Apply dynamic variables to a match profile.
        
        Args:
            profile: The match profile
            channel_name: Channel name variable value
            channel_group: Channel group variable value
            m3u_account_name: M3U account name variable value
            
        Returns:
            MatchProfile with variables replaced in patterns
        """
        variables = {
            '{channel_name}': channel_name,
            '{channel_group}': channel_group,
            '{m3u_account_name}': m3u_account_name
        }
        
        # Create a copy with resolved patterns
        resolved_steps = []
        for step in profile.steps:
            resolved_pattern = step.pattern
            for var, value in variables.items():
                resolved_pattern = resolved_pattern.replace(var, value)
            
            resolved_step = MatchProfileStep(
                id=step.id,
                type=step.type,
                pattern=resolved_pattern,
                variables=step.variables,
                enabled=step.enabled,
                order=step.order
            )
            resolved_steps.append(resolved_step)
        
        return MatchProfile(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            steps=resolved_steps,
            enabled=profile.enabled,
            created_at=profile.created_at,
            updated_at=profile.updated_at
        )
    
    def test_profile_against_stream(self, profile: MatchProfile, 
                                    stream_name: str = "",
                                    stream_url: str = "",
                                    stream_tvg_id: str = "") -> Dict[str, Any]:
        """
        Test a match profile against a stream to see if it matches.
        
        Args:
            profile: The match profile to test
            stream_name: Stream name
            stream_url: Stream URL
            stream_tvg_id: Stream TVG-ID
            
        Returns:
            Dict with match result and details
        """
        if not profile.enabled:
            return {
                'matched': False,
                'reason': 'Profile is disabled',
                'steps_results': []
            }
        
        steps_results = []
        overall_match = False
        
        for step in sorted(profile.steps, key=lambda s: s.order):
            if not step.enabled:
                steps_results.append({
                    'step_id': step.id,
                    'type': step.type,
                    'pattern': step.pattern,
                    'matched': False,
                    'reason': 'Step is disabled'
                })
                continue
            
            matched = False
            reason = ""
            
            try:
                if step.type == 'regex_name':
                    # Regex match on stream name
                    if re.search(step.pattern, stream_name, re.IGNORECASE):
                        matched = True
                        reason = f"Stream name '{stream_name}' matches pattern '{step.pattern}'"
                    else:
                        reason = f"Stream name '{stream_name}' does not match pattern '{step.pattern}'"
                
                elif step.type == 'tvg_id':
                    # Exact match on TVG-ID
                    if step.pattern == stream_tvg_id:
                        matched = True
                        reason = f"TVG-ID '{stream_tvg_id}' matches '{step.pattern}'"
                    else:
                        reason = f"TVG-ID '{stream_tvg_id}' does not match '{step.pattern}'"
                
                elif step.type == 'regex_url':
                    # Regex match on stream URL
                    if re.search(step.pattern, stream_url, re.IGNORECASE):
                        matched = True
                        reason = f"Stream URL matches pattern '{step.pattern}'"
                    else:
                        reason = f"Stream URL does not match pattern '{step.pattern}'"
                
                else:
                    reason = f"Unknown step type: {step.type}"
            
            except re.error as e:
                matched = False
                reason = f"Invalid regex pattern: {e}"
            
            steps_results.append({
                'step_id': step.id,
                'type': step.type,
                'pattern': step.pattern,
                'matched': matched,
                'reason': reason
            })
            
            # If any step matches, overall match is true (OR logic)
            if matched:
                overall_match = True
        
        return {
            'matched': overall_match,
            'reason': 'At least one step matched' if overall_match else 'No steps matched',
            'steps_results': steps_results
        }


# Global instance and lock for thread-safe singleton
_match_profiles_manager = None
_manager_lock = threading.Lock()


def get_match_profiles_manager(storage: Optional[Any] = None) -> MatchProfilesManager:
    """
    Get the global MatchProfilesManager instance (thread-safe singleton).
    
    Args:
        storage: Optional UDIStorage instance
        
    Returns:
        MatchProfilesManager instance
    """
    global _match_profiles_manager
    if _match_profiles_manager is None:
        with _manager_lock:
            # Double-check locking pattern
            if _match_profiles_manager is None:
                _match_profiles_manager = MatchProfilesManager(storage)
    return _match_profiles_manager
