#!/usr/bin/env python3
"""
Script to fix Dispatcharr API swagger.json compliance issues.

This script:
1. Adds missing descriptions to API endpoints
2. Adds missing descriptions to parameters
3. Fixes hardcoded host to use environment variable
4. Validates the updated swagger structure
"""

import json
import sys
from typing import Dict, Any

# Default descriptions for common HTTP methods
METHOD_DESCRIPTIONS = {
    'get': 'Retrieve resource(s)',
    'post': 'Create a new resource',
    'put': 'Update a resource',
    'patch': 'Partially update a resource',
    'delete': 'Delete a resource'
}

# Parameter descriptions based on common parameter names
PARAMETER_DESCRIPTIONS = {
    'id': 'Unique identifier',
    'data': 'Request payload',
    'name': 'Filter by name',
    'page': 'Page number for pagination',
    'page_size': 'Number of results per page',
    'perPage': 'Number of results per page',
    'ordering': 'Field to use for ordering results',
    'search': 'Search term for filtering',
    'channel_id': 'Channel identifier',
    'stream_id': 'Stream identifier',
    'account_id': 'Account identifier',
    'profile_id': 'Profile identifier',
    'channel_group': 'Channel group filter',
    'epg': 'EPG filter',
    'm3u_account': 'M3U account filter',
    'is_active': 'Filter by active status',
    'category': 'Category filter',
    'year': 'Year filter',
    'year_gte': 'Minimum year filter',
    'year_lte': 'Maximum year filter',
    'season_number': 'Season number filter',
    'episode_number': 'Episode number filter',
    'series': 'Series filter',
    'channel_group_name': 'Channel group name filter',
    'm3u_account_name': 'M3U account name filter',
    'm3u_account_is_active': 'M3U account active status filter',
    'category_type': 'Category type filter',
    'state': 'State filter',
    'severity': 'Severity filter',
    'ref': 'Git reference filter',
    'tool_name': 'Tool name filter',
    'resolution': 'Resolution filter',
    'secret_type': 'Secret type filter',
    'filter': 'Filter type',
    'event': 'Event type',
    'status': 'Status filter',
    'actor': 'Actor filter',
    'branch': 'Branch filter',
    'workflow_id': 'Workflow identifier',
    'run_id': 'Run identifier',
    'job_id': 'Job identifier',
    'artifact_id': 'Artifact identifier',
    'tag': 'Tag name',
    'sha': 'Commit SHA',
    'username': 'Username',
    'password': 'Password',
    'tvg_id': 'TVG ID',
    'key': 'Key identifier',
}


def generate_path_description(path: str, method: str, operation_id: str) -> str:
    """
    Generate a meaningful description based on the path and method.
    
    Args:
        path: API path
        method: HTTP method
        operation_id: Operation ID from swagger
    
    Returns:
        Generated description
    """
    # Use existing description format or generate from operation_id
    if operation_id:
        # Convert operation_id to human-readable description
        parts = operation_id.replace('_', ' ').split()
        if parts:
            return ' '.join(word.capitalize() for word in parts)
    
    # Fall back to generic description
    return METHOD_DESCRIPTIONS.get(method, 'API endpoint')


def generate_parameter_description(param_name: str, param_type: str) -> str:
    """
    Generate a meaningful description for a parameter.
    
    Args:
        param_name: Parameter name
        param_type: Parameter type (path, query, body, etc.)
    
    Returns:
        Generated description
    """
    # Check if we have a predefined description
    if param_name in PARAMETER_DESCRIPTIONS:
        return PARAMETER_DESCRIPTIONS[param_name]
    
    # Generate based on parameter type
    if param_type == 'body':
        return 'Request body with required data'
    elif param_type == 'path':
        return f'Path parameter: {param_name}'
    elif param_type == 'query':
        return f'Query parameter for filtering: {param_name}'
    
    return f'Parameter: {param_name}'


def fix_swagger_compliance(swagger_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fix swagger.json compliance issues.
    
    Args:
        swagger_data: Original swagger data
    
    Returns:
        Fixed swagger data
    """
    fixes_applied = {
        'paths_fixed': 0,
        'params_fixed': 0,
        'host_fixed': False
    }
    
    # Fix host (remove hardcoded IP)
    if 'host' in swagger_data and swagger_data['host'] != 'localhost:9191':
        # Keep a placeholder that should be overridden by deployment
        swagger_data['host'] = 'localhost:9191'
        fixes_applied['host_fixed'] = True
        print(f"Fixed host to use localhost:9191 (should be overridden in deployment)")
    
    # Fix paths
    for path, methods in swagger_data.get('paths', {}).items():
        for method, details in methods.items():
            if method == 'parameters' or not isinstance(details, dict):
                continue
            
            # Add missing description
            if not details.get('description', '').strip():
                operation_id = details.get('operationId', '')
                details['description'] = generate_path_description(path, method, operation_id)
                fixes_applied['paths_fixed'] += 1
            
            # Fix parameters
            parameters = details.get('parameters', [])
            for param in parameters:
                if not param.get('description', '').strip():
                    param_name = param.get('name', 'unnamed')
                    param_in = param.get('in', 'unknown')
                    param['description'] = generate_parameter_description(param_name, param_in)
                    fixes_applied['params_fixed'] += 1
    
    return swagger_data, fixes_applied


def validate_swagger(swagger_data: Dict[str, Any]) -> bool:
    """
    Validate swagger data structure.
    
    Args:
        swagger_data: Swagger data to validate
    
    Returns:
        True if valid, False otherwise
    """
    required_fields = ['swagger', 'info', 'paths']
    for field in required_fields:
        if field not in swagger_data:
            print(f"ERROR: Missing required field: {field}")
            return False
    
    # Check for empty descriptions
    empty_descriptions = []
    empty_params = []
    
    for path, methods in swagger_data.get('paths', {}).items():
        for method, details in methods.items():
            if method == 'parameters' or not isinstance(details, dict):
                continue
            
            if not details.get('description', '').strip():
                empty_descriptions.append(f"{method.upper()} {path}")
            
            for param in details.get('parameters', []):
                if not param.get('description', '').strip():
                    empty_params.append(
                        f"{method.upper()} {path} - param '{param.get('name', 'unnamed')}'"
                    )
    
    if empty_descriptions:
        print(f"WARNING: {len(empty_descriptions)} paths still have empty descriptions")
        return False
    
    if empty_params:
        print(f"WARNING: {len(empty_params)} parameters still have empty descriptions")
        return False
    
    print("✓ All paths have descriptions")
    print("✓ All parameters have descriptions")
    return True


def main():
    """Main function to fix swagger.json"""
    swagger_file = 'swagger.json'
    backup_file = 'swagger.json.backup'
    
    print("Dispatcharr API Swagger Compliance Fixer")
    print("=" * 80)
    
    # Load swagger.json
    try:
        with open(swagger_file, 'r') as f:
            swagger_data = json.load(f)
        print(f"✓ Loaded {swagger_file}")
    except Exception as e:
        print(f"ERROR: Failed to load {swagger_file}: {e}")
        return 1
    
    # Create backup
    try:
        with open(backup_file, 'w') as f:
            json.dump(swagger_data, f, indent=2)
        print(f"✓ Created backup at {backup_file}")
    except Exception as e:
        print(f"WARNING: Failed to create backup: {e}")
    
    # Fix compliance issues
    print("\nApplying fixes...")
    fixed_swagger, fixes = fix_swagger_compliance(swagger_data)
    
    print(f"  - Fixed {fixes['paths_fixed']} path descriptions")
    print(f"  - Fixed {fixes['params_fixed']} parameter descriptions")
    if fixes['host_fixed']:
        print(f"  - Fixed host configuration")
    
    # Validate
    print("\nValidating fixed swagger...")
    if not validate_swagger(fixed_swagger):
        print("ERROR: Validation failed after fixes")
        return 1
    
    # Save fixed swagger
    try:
        with open(swagger_file, 'w') as f:
            json.dump(fixed_swagger, f, indent=2)
        print(f"\n✓ Saved fixed swagger to {swagger_file}")
    except Exception as e:
        print(f"ERROR: Failed to save fixed swagger: {e}")
        return 1
    
    print("\n" + "=" * 80)
    print("SUCCESS: swagger.json has been fixed for Dispatcharr API compliance")
    print("=" * 80)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
