#!/usr/bin/env python3
"""
Match Profile Manager

Manages match profiles for advanced stream-to-channel matching.
Profiles define reusable matching pipelines with filters, transformations, and actions.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from logging_config import setup_logging

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
MATCH_PROFILES_FILE = CONFIG_DIR / 'match_profiles.json'


class MatchProfile:
    """Represents a single match profile with pipeline configuration."""
    
    def __init__(self, data: Dict[str, Any]):
        """Initialize match profile from data dictionary.
        
        Args:
            data: Profile configuration dictionary
        """
        self.id = data.get('id')
        self.name = data.get('name', '')
        self.description = data.get('description', '')
        self.enabled = data.get('enabled', True)
        self.priority = data.get('priority', 100)
        self.pipeline = data.get('pipeline', {'nodes': [], 'edges': []})
        self.stats = data.get('stats', {
            'last_run': None,
            'streams_matched': 0,
            'channels_updated': 0
        })
        self.created_at = data.get('created_at', datetime.now().isoformat())
        self.updated_at = data.get('updated_at', datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert profile to dictionary for serialization.
        
        Returns:
            Dictionary representation of the profile
        """
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'enabled': self.enabled,
            'priority': self.priority,
            'pipeline': self.pipeline,
            'stats': self.stats,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }
    
    def validate(self) -> Tuple[bool, List[str]]:
        """Validate profile configuration.
        
        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []
        
        # Check required fields
        if not self.name:
            errors.append("Profile name is required")
        
        if not isinstance(self.priority, int) or self.priority < 0:
            errors.append("Priority must be a non-negative integer")
        
        # Validate pipeline structure
        if not isinstance(self.pipeline, dict):
            errors.append("Pipeline must be a dictionary")
        else:
            nodes = self.pipeline.get('nodes', [])
            edges = self.pipeline.get('edges', [])
            
            if not isinstance(nodes, list):
                errors.append("Pipeline nodes must be a list")
            
            if not isinstance(edges, list):
                errors.append("Pipeline edges must be a list")
            
            # Validate nodes
            node_ids = set()
            for i, node in enumerate(nodes):
                if not isinstance(node, dict):
                    errors.append(f"Node {i} must be a dictionary")
                    continue
                
                node_id = node.get('id')
                if not node_id:
                    errors.append(f"Node {i} missing required field: id")
                else:
                    if node_id in node_ids:
                        errors.append(f"Duplicate node ID: {node_id}")
                    node_ids.add(node_id)
                
                if 'type' not in node:
                    errors.append(f"Node {node_id} missing required field: type")
                elif node['type'] not in ['source', 'filter', 'transform', 'match', 'action']:
                    errors.append(f"Node {node_id} has invalid type: {node['type']}")
                
                if 'config' not in node:
                    errors.append(f"Node {node_id} missing required field: config")
            
            # Validate edges
            for i, edge in enumerate(edges):
                if not isinstance(edge, dict):
                    errors.append(f"Edge {i} must be a dictionary")
                    continue
                
                if 'from' not in edge:
                    errors.append(f"Edge {i} missing required field: from")
                elif edge['from'] not in node_ids:
                    errors.append(f"Edge {i} references non-existent node: {edge['from']}")
                
                if 'to' not in edge:
                    errors.append(f"Edge {i} missing required field: to")
                elif edge['to'] not in node_ids:
                    errors.append(f"Edge {i} references non-existent node: {edge['to']}")
        
        return (len(errors) == 0, errors)
    
    def update_stats(self, streams_matched: int, channels_updated: int):
        """Update profile execution statistics.
        
        Args:
            streams_matched: Number of streams matched in this execution
            channels_updated: Number of channels updated in this execution
        """
        self.stats['last_run'] = datetime.now().isoformat()
        self.stats['streams_matched'] = streams_matched
        self.stats['channels_updated'] = channels_updated
        self.updated_at = datetime.now().isoformat()


class MatchProfileManager:
    """Manages match profiles for stream matching."""
    
    def __init__(self, config_dir: Optional[Path] = None):
        """Initialize the match profile manager.
        
        Args:
            config_dir: Optional configuration directory path
        """
        self._lock = threading.Lock()
        self._profiles: Dict[int, MatchProfile] = {}
        self._next_id = 1
        
        # Set config directory
        global CONFIG_DIR, MATCH_PROFILES_FILE
        if config_dir:
            CONFIG_DIR = Path(config_dir)
            MATCH_PROFILES_FILE = CONFIG_DIR / 'match_profiles.json'
        
        self._load_profiles()
        logger.info("Match profile manager initialized")
    
    def _load_profiles(self) -> None:
        """Load profiles from file."""
        try:
            if MATCH_PROFILES_FILE.exists():
                with open(MATCH_PROFILES_FILE, 'r') as f:
                    data = json.load(f)
                    profiles_data = data.get('profiles', [])
                    
                    for profile_data in profiles_data:
                        profile = MatchProfile(profile_data)
                        self._profiles[profile.id] = profile
                        if profile.id >= self._next_id:
                            self._next_id = profile.id + 1
                    
                    logger.info(f"Loaded {len(self._profiles)} match profiles from file")
            else:
                # Create default empty file
                self._save_profiles()
                logger.info("Created default match profiles configuration")
        except Exception as e:
            logger.error(f"Error loading match profiles: {e}", exc_info=True)
            self._profiles = {}
    
    def _save_profiles(self) -> bool:
        """Save profiles to file.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            
            data = {
                'profiles': [profile.to_dict() for profile in self._profiles.values()],
                'metadata': {
                    'version': '1.0',
                    'last_updated': datetime.now().isoformat()
                }
            }
            
            with open(MATCH_PROFILES_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Saved {len(self._profiles)} match profiles to file")
            return True
        except Exception as e:
            logger.error(f"Error saving match profiles: {e}", exc_info=True)
            return False
    
    def get_all_profiles(self) -> List[Dict[str, Any]]:
        """Get all profiles.
        
        Returns:
            List of profile dictionaries
        """
        with self._lock:
            return [profile.to_dict() for profile in self._profiles.values()]
    
    def get_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific profile by ID.
        
        Args:
            profile_id: Profile ID
            
        Returns:
            Profile dictionary or None if not found
        """
        with self._lock:
            profile = self._profiles.get(profile_id)
            return profile.to_dict() if profile else None
    
    def create_profile(self, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new profile.
        
        Args:
            profile_data: Profile configuration
            
        Returns:
            Created profile dictionary
            
        Raises:
            ValueError: If profile data is invalid
        """
        with self._lock:
            # Assign new ID
            profile_data['id'] = self._next_id
            self._next_id += 1
            
            # Set timestamps
            profile_data['created_at'] = datetime.now().isoformat()
            profile_data['updated_at'] = datetime.now().isoformat()
            
            # Create profile
            profile = MatchProfile(profile_data)
            
            # Validate
            is_valid, errors = profile.validate()
            if not is_valid:
                raise ValueError(f"Invalid profile: {'; '.join(errors)}")
            
            # Save
            self._profiles[profile.id] = profile
            self._save_profiles()
            
            logger.info(f"Created match profile: {profile.name} (ID: {profile.id})")
            return profile.to_dict()
    
    def update_profile(self, profile_id: int, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing profile.
        
        Args:
            profile_id: Profile ID
            profile_data: Updated profile configuration
            
        Returns:
            Updated profile dictionary
            
        Raises:
            ValueError: If profile doesn't exist or data is invalid
        """
        with self._lock:
            if profile_id not in self._profiles:
                raise ValueError(f"Profile {profile_id} not found")
            
            # Preserve ID and created_at
            profile_data['id'] = profile_id
            profile_data['created_at'] = self._profiles[profile_id].created_at
            profile_data['updated_at'] = datetime.now().isoformat()
            
            # Create updated profile
            profile = MatchProfile(profile_data)
            
            # Validate
            is_valid, errors = profile.validate()
            if not is_valid:
                raise ValueError(f"Invalid profile: {'; '.join(errors)}")
            
            # Save
            self._profiles[profile_id] = profile
            self._save_profiles()
            
            logger.info(f"Updated match profile: {profile.name} (ID: {profile.id})")
            return profile.to_dict()
    
    def delete_profile(self, profile_id: int) -> bool:
        """Delete a profile.
        
        Args:
            profile_id: Profile ID
            
        Returns:
            True if successful, False if profile doesn't exist
        """
        with self._lock:
            if profile_id not in self._profiles:
                return False
            
            profile_name = self._profiles[profile_id].name
            del self._profiles[profile_id]
            self._save_profiles()
            
            logger.info(f"Deleted match profile: {profile_name} (ID: {profile_id})")
            return True
    
    def get_enabled_profiles(self) -> List[MatchProfile]:
        """Get all enabled profiles sorted by priority (highest first).
        
        Returns:
            List of enabled MatchProfile objects sorted by priority
        """
        with self._lock:
            enabled = [p for p in self._profiles.values() if p.enabled]
            return sorted(enabled, key=lambda p: p.priority, reverse=True)
    
    def validate_profile(self, profile_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate a profile configuration without saving.
        
        Args:
            profile_data: Profile configuration to validate
            
        Returns:
            Tuple of (is_valid, list of error messages)
        """
        try:
            profile = MatchProfile(profile_data)
            return profile.validate()
        except Exception as e:
            return (False, [str(e)])
    
    def update_profile_stats(self, profile_id: int, streams_matched: int, channels_updated: int) -> bool:
        """Update statistics for a profile after execution.
        
        Args:
            profile_id: Profile ID
            streams_matched: Number of streams matched
            channels_updated: Number of channels updated
            
        Returns:
            True if successful, False if profile doesn't exist
        """
        with self._lock:
            if profile_id not in self._profiles:
                return False
            
            self._profiles[profile_id].update_stats(streams_matched, channels_updated)
            self._save_profiles()
            return True


# Global singleton instance
_match_profile_manager: Optional[MatchProfileManager] = None
_manager_lock = threading.Lock()


def get_match_profile_manager(config_dir: Optional[Path] = None) -> MatchProfileManager:
    """Get the global match profile manager singleton instance.
    
    Args:
        config_dir: Optional configuration directory (only used on first call)
    
    Returns:
        The match profile manager instance
    """
    global _match_profile_manager
    with _manager_lock:
        if _match_profile_manager is None:
            _match_profile_manager = MatchProfileManager(config_dir)
        return _match_profile_manager
