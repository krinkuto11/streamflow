#!/usr/bin/env python3
"""
Match Profile Pipeline Executor

Executes match profile pipelines to match streams to channels.
Processes pipeline nodes (Source, Filter, Transform, Match, Action) in sequence.
"""

import re
from typing import Dict, List, Optional, Any, Set
from collections import defaultdict

from logging_config import setup_logging
from match_profile_manager import MatchProfile, get_match_profile_manager

logger = setup_logging(__name__)


def normalize_pattern_spaces(pattern: str) -> str:
    """Convert literal spaces in pattern to flexible whitespace regex.
    
    This allows matching streams with different whitespace characters
    (non-breaking spaces, tabs, double spaces, etc.).
    
    Args:
        pattern: Regex pattern string
        
    Returns:
        Pattern with spaces converted to \\s+
    """
    return re.sub(r' +', r'\\s+', pattern)


class PipelineNode:
    """Base class for pipeline nodes."""
    
    def __init__(self, node_id: str, config: Dict[str, Any]):
        """Initialize pipeline node.
        
        Args:
            node_id: Unique node identifier
            config: Node configuration dictionary
        """
        self.node_id = node_id
        self.config = config
    
    def execute(self, input_data: Any, context: Dict[str, Any]) -> Any:
        """Execute the node logic.
        
        Args:
            input_data: Input data from previous node
            context: Execution context with shared data
            
        Returns:
            Output data for next node
        """
        raise NotImplementedError("Subclasses must implement execute()")


class SourceNode(PipelineNode):
    """Source node - filters streams by M3U accounts and groups."""
    
    def execute(self, input_data: List[Dict], context: Dict[str, Any]) -> List[Dict]:
        """Filter streams based on source configuration.
        
        Args:
            input_data: List of all available streams
            context: Execution context
            
        Returns:
            Filtered list of streams
        """
        logger.debug(f"Executing source node {self.node_id}")
        
        m3u_accounts = self.config.get('m3u_accounts', [])
        stream_groups = self.config.get('stream_groups', [])
        
        filtered_streams = []
        
        for stream in input_data:
            # Filter by M3U account if specified
            if m3u_accounts:
                if stream.get('m3u_account') not in m3u_accounts:
                    continue
            
            # Filter by stream group if specified
            if stream_groups:
                stream_group = stream.get('channel_group_name', '')
                if not any(group.lower() in stream_group.lower() for group in stream_groups):
                    continue
            
            filtered_streams.append(stream)
        
        logger.debug(f"Source node filtered {len(input_data)} streams to {len(filtered_streams)}")
        return filtered_streams


class FilterNode(PipelineNode):
    """Filter node - filters streams by patterns and attributes."""
    
    def execute(self, input_data: List[Dict], context: Dict[str, Any]) -> List[Dict]:
        """Filter streams based on patterns and criteria.
        
        Args:
            input_data: List of streams from previous node
            context: Execution context
            
        Returns:
            Filtered list of streams
        """
        logger.debug(f"Executing filter node {self.node_id}")
        
        patterns = self.config.get('patterns', [])
        exclude_dead = self.config.get('exclude_dead', True)
        case_sensitive = self.config.get('case_sensitive', False)
        
        filtered_streams = []
        
        for stream in input_data:
            stream_name = stream.get('name', '')
            
            # Skip dead streams if configured
            if exclude_dead and '[DEAD]' in stream_name:
                continue
            
            # If patterns specified, stream must match at least one
            if patterns:
                search_name = stream_name if case_sensitive else stream_name.lower()
                matched = False
                
                for pattern in patterns:
                    search_pattern = pattern if case_sensitive else pattern.lower()
                    # Convert literal spaces to flexible whitespace
                    search_pattern = normalize_pattern_spaces(search_pattern)
                    
                    try:
                        if re.search(search_pattern, search_name):
                            matched = True
                            break
                    except re.error as e:
                        logger.warning(f"Invalid pattern '{pattern}': {e}")
                        continue
                
                if not matched:
                    continue
            
            filtered_streams.append(stream)
        
        logger.debug(f"Filter node filtered {len(input_data)} streams to {len(filtered_streams)}")
        return filtered_streams


class TransformNode(PipelineNode):
    """Transform node - transforms stream names before matching."""
    
    def execute(self, input_data: List[Dict], context: Dict[str, Any]) -> List[Dict]:
        """Transform stream names.
        
        Args:
            input_data: List of streams from previous node
            context: Execution context
            
        Returns:
            List of streams with transformed names (in context)
        """
        logger.debug(f"Executing transform node {self.node_id}")
        
        remove_prefixes = self.config.get('remove_prefixes', [])
        remove_suffixes = self.config.get('remove_suffixes', [])
        normalize_whitespace = self.config.get('normalize_whitespace', False)
        
        # Store original names in context
        if 'original_names' not in context:
            context['original_names'] = {}
        
        transformed_streams = []
        
        for stream in input_data.copy():  # Work on copy
            original_name = stream['name']
            transformed_name = original_name
            
            # Remove prefixes
            for prefix in remove_prefixes:
                if transformed_name.startswith(prefix):
                    transformed_name = transformed_name[len(prefix):]
            
            # Remove suffixes
            for suffix in remove_suffixes:
                if transformed_name.endswith(suffix):
                    transformed_name = transformed_name[:-len(suffix)]
            
            # Normalize whitespace
            if normalize_whitespace:
                transformed_name = ' '.join(transformed_name.split())
            
            # Store transformation mapping
            stream_copy = stream.copy()
            stream_copy['name'] = transformed_name
            context['original_names'][stream['id']] = original_name
            
            transformed_streams.append(stream_copy)
        
        logger.debug(f"Transform node processed {len(transformed_streams)} streams")
        return transformed_streams


class MatchNode(PipelineNode):
    """Match node - matches streams to channels."""
    
    def execute(self, input_data: List[Dict], context: Dict[str, Any]) -> Dict[int, List[int]]:
        """Match streams to channels.
        
        Args:
            input_data: List of streams from previous node
            context: Execution context
            
        Returns:
            Dictionary mapping stream IDs to lists of matched channel IDs
        """
        logger.debug(f"Executing match node {self.node_id}")
        
        channels = self.config.get('channels', [])
        match_mode = self.config.get('match_mode', 'regex')
        patterns = self.config.get('patterns', {})
        case_sensitive = self.config.get('case_sensitive', False)
        
        matches: Dict[int, List[int]] = {}
        
        for stream in input_data:
            stream_id = stream['id']
            stream_name = stream['name']
            search_name = stream_name if case_sensitive else stream_name.lower()
            
            matched_channels = []
            
            # Check each configured channel
            for channel_id in channels:
                channel_patterns = patterns.get(str(channel_id), [])
                
                if not channel_patterns:
                    continue
                
                for pattern in channel_patterns:
                    search_pattern = pattern if case_sensitive else pattern.lower()
                    
                    if match_mode == 'regex':
                        # Convert literal spaces to flexible whitespace
                        search_pattern = normalize_pattern_spaces(search_pattern)
                        
                        try:
                            if re.search(search_pattern, search_name):
                                matched_channels.append(channel_id)
                                break  # Only match once per channel
                        except re.error as e:
                            logger.warning(f"Invalid pattern '{pattern}' for channel {channel_id}: {e}")
                    
                    elif match_mode == 'exact':
                        if search_pattern == search_name:
                            matched_channels.append(channel_id)
                            break
                    
                    elif match_mode == 'contains':
                        if search_pattern in search_name:
                            matched_channels.append(channel_id)
                            break
            
            if matched_channels:
                matches[stream_id] = matched_channels
        
        logger.debug(f"Match node matched {len(matches)} streams to channels")
        return matches


class ActionNode(PipelineNode):
    """Action node - performs actions on matched streams."""
    
    def execute(self, input_data: Dict[int, List[int]], context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute actions on matched streams.
        
        Args:
            input_data: Dictionary mapping stream IDs to channel IDs
            context: Execution context
            
        Returns:
            Dictionary with action results
        """
        logger.debug(f"Executing action node {self.node_id}")
        
        action = self.config.get('action', 'add_to_channel')
        deduplicate = self.config.get('deduplicate', True)
        max_streams_per_channel = self.config.get('max_streams_per_channel', 0)
        
        # Store matches for later application
        context['matches'] = input_data
        context['action_config'] = {
            'action': action,
            'deduplicate': deduplicate,
            'max_streams_per_channel': max_streams_per_channel
        }
        
        # Count stats
        total_matches = sum(len(channels) for channels in input_data.values())
        unique_channels = len(set(ch for channels in input_data.values() for ch in channels))
        
        result = {
            'streams_matched': len(input_data),
            'total_matches': total_matches,
            'channels_affected': unique_channels,
            'action': action
        }
        
        logger.debug(f"Action node processed {len(input_data)} stream matches")
        return result


class MatchProfileExecutor:
    """Executes match profile pipelines."""
    
    NODE_TYPES = {
        'source': SourceNode,
        'filter': FilterNode,
        'transform': TransformNode,
        'match': MatchNode,
        'action': ActionNode
    }
    
    def __init__(self, udi_manager=None):
        """Initialize the match profile executor.
        
        Args:
            udi_manager: UDI manager for data access (optional, for future use)
        """
        self.udi_manager = udi_manager
        self.profile_manager = get_match_profile_manager()
        logger.info("Match profile executor initialized")
    
    def _build_execution_order(self, pipeline: Dict) -> List[str]:
        """Build execution order for pipeline nodes.
        
        Args:
            pipeline: Pipeline configuration with nodes and edges
            
        Returns:
            List of node IDs in execution order
        """
        nodes = {node['id']: node for node in pipeline.get('nodes', [])}
        edges = pipeline.get('edges', [])
        
        # Build dependency graph
        dependencies: Dict[str, List[str]] = defaultdict(list)
        incoming_count: Dict[str, int] = {node_id: 0 for node_id in nodes.keys()}
        
        for edge in edges:
            from_node = edge['from']
            to_node = edge['to']
            dependencies[from_node].append(to_node)
            incoming_count[to_node] = incoming_count.get(to_node, 0) + 1
        
        # Topological sort
        queue = [node_id for node_id, count in incoming_count.items() if count == 0]
        execution_order = []
        
        while queue:
            node_id = queue.pop(0)
            execution_order.append(node_id)
            
            for dependent in dependencies.get(node_id, []):
                incoming_count[dependent] -= 1
                if incoming_count[dependent] == 0:
                    queue.append(dependent)
        
        # Check for cycles
        if len(execution_order) != len(nodes):
            raise ValueError("Pipeline contains cycles")
        
        return execution_order
    
    def _create_node(self, node_config: Dict) -> PipelineNode:
        """Create a pipeline node from configuration.
        
        Args:
            node_config: Node configuration dictionary
            
        Returns:
            Instantiated pipeline node
            
        Raises:
            ValueError: If node type is unknown
        """
        node_type = node_config['type']
        node_class = self.NODE_TYPES.get(node_type)
        
        if not node_class:
            raise ValueError(f"Unknown node type: {node_type}")
        
        return node_class(node_config['id'], node_config.get('config', {}))
    
    def execute_profile(self, profile: MatchProfile, streams: List[Dict]) -> Dict[str, Any]:
        """Execute a single match profile.
        
        Args:
            profile: Match profile to execute
            streams: List of available streams
            
        Returns:
            Execution results with matches and statistics
        """
        logger.info(f"Executing match profile: {profile.name} (ID: {profile.id})")
        
        try:
            # Build execution order
            execution_order = self._build_execution_order(profile.pipeline)
            
            # Create nodes
            nodes = {node['id']: self._create_node(node) for node in profile.pipeline.get('nodes', [])}
            
            # Execute pipeline
            context: Dict[str, Any] = {}
            current_data = streams
            
            for node_id in execution_order:
                node = nodes[node_id]
                current_data = node.execute(current_data, context)
            
            # Extract results
            matches = context.get('matches', {})
            action_config = context.get('action_config', {})
            
            # Build result
            result = {
                'profile_id': profile.id,
                'profile_name': profile.name,
                'matches': matches,
                'action_config': action_config,
                'streams_matched': len(matches),
                'channels_affected': len(set(ch for channels in matches.values() for ch in channels)),
                'success': True
            }
            
            logger.info(f"Profile '{profile.name}' matched {len(matches)} streams to channels")
            return result
            
        except Exception as e:
            logger.error(f"Error executing profile '{profile.name}': {e}", exc_info=True)
            return {
                'profile_id': profile.id,
                'profile_name': profile.name,
                'matches': {},
                'streams_matched': 0,
                'channels_affected': 0,
                'success': False,
                'error': str(e)
            }
    
    def execute_all_profiles(self, streams: List[Dict]) -> Dict[str, Any]:
        """Execute all enabled match profiles.
        
        Args:
            streams: List of available streams
            
        Returns:
            Combined execution results from all profiles
        """
        enabled_profiles = self.profile_manager.get_enabled_profiles()
        
        if not enabled_profiles:
            logger.debug("No enabled match profiles to execute")
            return {
                'total_streams_matched': 0,
                'total_channels_affected': 0,
                'profile_results': [],
                'combined_matches': {}
            }
        
        logger.info(f"Executing {len(enabled_profiles)} enabled match profiles")
        
        # Track which streams have been matched
        matched_streams: Set[int] = set()
        combined_matches: Dict[int, List[int]] = {}
        profile_results = []
        
        # Execute profiles in priority order (already sorted by get_enabled_profiles)
        for profile in enabled_profiles:
            # Filter out already-matched streams if profiles should be exclusive
            # For now, we allow multiple profiles to match the same stream
            # The first (highest priority) match will be used
            
            result = self.execute_profile(profile, streams)
            profile_results.append(result)
            
            if result['success']:
                # Add matches to combined results (priority determines which profile wins)
                for stream_id, channel_ids in result['matches'].items():
                    if stream_id not in matched_streams:
                        combined_matches[stream_id] = channel_ids
                        matched_streams.add(stream_id)
                
                # Update profile statistics
                self.profile_manager.update_profile_stats(
                    profile.id,
                    result['streams_matched'],
                    result['channels_affected']
                )
        
        # Build combined result
        total_channels = len(set(ch for channels in combined_matches.values() for ch in channels))
        
        combined_result = {
            'total_streams_matched': len(combined_matches),
            'total_channels_affected': total_channels,
            'profile_results': profile_results,
            'combined_matches': combined_matches
        }
        
        logger.info(f"All profiles executed: {len(combined_matches)} streams matched to {total_channels} channels")
        return combined_result
    
    def test_profile(self, profile: MatchProfile, streams: List[Dict]) -> Dict[str, Any]:
        """Test a profile without applying changes.
        
        Args:
            profile: Match profile to test
            streams: List of streams to test against
            
        Returns:
            Test results with preview of matches
        """
        logger.info(f"Testing match profile: {profile.name} (ID: {profile.id})")
        
        # Execute profile (doesn't actually apply changes)
        result = self.execute_profile(profile, streams)
        
        # Add stream details to results for preview
        stream_lookup = {s['id']: s for s in streams}
        
        preview_matches = []
        for stream_id, channel_ids in result.get('matches', {}).items():
            stream = stream_lookup.get(stream_id)
            if stream:
                preview_matches.append({
                    'stream_id': stream_id,
                    'stream_name': stream.get('name'),
                    'channel_ids': channel_ids
                })
        
        result['preview_matches'] = preview_matches
        return result
    
    @staticmethod
    def get_node_types() -> Dict[str, Any]:
        """Get available node types and their configuration schemas.
        
        Returns:
            Dictionary of node type definitions
        """
        return {
            'source': {
                'name': 'Source',
                'description': 'Filter streams by M3U accounts and groups',
                'config_schema': {
                    'm3u_accounts': {'type': 'array', 'description': 'List of M3U account IDs'},
                    'stream_groups': {'type': 'array', 'description': 'List of stream group names'}
                }
            },
            'filter': {
                'name': 'Filter',
                'description': 'Filter streams by patterns and attributes',
                'config_schema': {
                    'patterns': {'type': 'array', 'description': 'List of regex patterns'},
                    'exclude_dead': {'type': 'boolean', 'description': 'Exclude dead streams'},
                    'case_sensitive': {'type': 'boolean', 'description': 'Case-sensitive matching'}
                }
            },
            'transform': {
                'name': 'Transform',
                'description': 'Transform stream names before matching',
                'config_schema': {
                    'remove_prefixes': {'type': 'array', 'description': 'Prefixes to remove'},
                    'remove_suffixes': {'type': 'array', 'description': 'Suffixes to remove'},
                    'normalize_whitespace': {'type': 'boolean', 'description': 'Normalize whitespace'}
                }
            },
            'match': {
                'name': 'Match',
                'description': 'Match streams to channels',
                'config_schema': {
                    'channels': {'type': 'array', 'description': 'List of channel IDs'},
                    'match_mode': {'type': 'string', 'enum': ['regex', 'exact', 'contains']},
                    'patterns': {'type': 'object', 'description': 'Channel ID to patterns mapping'},
                    'case_sensitive': {'type': 'boolean', 'description': 'Case-sensitive matching'}
                }
            },
            'action': {
                'name': 'Action',
                'description': 'Perform actions on matched streams',
                'config_schema': {
                    'action': {'type': 'string', 'enum': ['add_to_channel', 'tag', 'skip']},
                    'deduplicate': {'type': 'boolean', 'description': 'Remove duplicate streams'},
                    'max_streams_per_channel': {'type': 'integer', 'description': 'Max streams per channel (0 = unlimited)'}
                }
            }
        }


# Global singleton instance
_match_profile_executor: Optional[MatchProfileExecutor] = None


def get_match_profile_executor(udi_manager=None) -> MatchProfileExecutor:
    """Get the global match profile executor singleton instance.
    
    Args:
        udi_manager: UDI manager instance (optional)
    
    Returns:
        The match profile executor instance
    """
    global _match_profile_executor
    if _match_profile_executor is None:
        _match_profile_executor = MatchProfileExecutor(udi_manager)
    return _match_profile_executor
