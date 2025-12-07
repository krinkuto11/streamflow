#!/usr/bin/env python3
"""
Validate swagger.json for Dispatcharr API compliance.

This script checks:
1. All required Swagger 2.0 fields are present
2. All paths have descriptions
3. All parameters have descriptions
4. Proper security definitions
5. Valid response codes and schemas
"""

import json
import sys
from typing import Dict, List, Tuple


def validate_required_fields(swagger_data: Dict) -> Tuple[bool, List[str]]:
    """Validate that all required Swagger 2.0 fields are present."""
    errors = []
    required_fields = {
        'swagger': str,
        'info': dict,
        'paths': dict,
    }
    
    for field, expected_type in required_fields.items():
        if field not in swagger_data:
            errors.append(f"Missing required field: {field}")
        elif not isinstance(swagger_data[field], expected_type):
            errors.append(f"Field '{field}' has wrong type")
    
    # Check info fields
    if 'info' in swagger_data:
        info_required = ['title', 'version']
        for field in info_required:
            if field not in swagger_data['info']:
                errors.append(f"Missing required info field: {field}")
    
    return len(errors) == 0, errors


def validate_paths(swagger_data: Dict) -> Tuple[bool, List[str]]:
    """Validate all paths have proper structure and descriptions."""
    errors = []
    warnings = []
    
    for path, methods in swagger_data.get('paths', {}).items():
        for method, details in methods.items():
            if method == 'parameters' or not isinstance(details, dict):
                continue
            
            # Check for description
            if not details.get('description', '').strip():
                errors.append(f"Missing description: {method.upper()} {path}")
            
            # Check for operationId
            if not details.get('operationId'):
                warnings.append(f"Missing operationId: {method.upper()} {path}")
            
            # Check for responses
            if not details.get('responses'):
                errors.append(f"Missing responses: {method.upper()} {path}")
            
            # Check for tags
            if not details.get('tags'):
                warnings.append(f"Missing tags: {method.upper()} {path}")
    
    return len(errors) == 0, errors + warnings


def validate_parameters(swagger_data: Dict) -> Tuple[bool, List[str]]:
    """Validate all parameters have descriptions."""
    errors = []
    
    for path, methods in swagger_data.get('paths', {}).items():
        for method, details in methods.items():
            if method == 'parameters' or not isinstance(details, dict):
                continue
            
            parameters = details.get('parameters', [])
            for param in parameters:
                param_name = param.get('name', 'unnamed')
                
                # Check for description
                if not param.get('description', '').strip():
                    errors.append(
                        f"Missing parameter description: {method.upper()} {path} - {param_name}"
                    )
                
                # Check for required fields
                if 'name' not in param:
                    errors.append(f"Parameter missing 'name': {method.upper()} {path}")
                
                if 'in' not in param:
                    errors.append(f"Parameter missing 'in': {method.upper()} {path} - {param_name}")
    
    return len(errors) == 0, errors


def validate_definitions(swagger_data: Dict) -> Tuple[bool, List[str]]:
    """Validate all definitions have proper structure."""
    errors = []
    
    for def_name, def_details in swagger_data.get('definitions', {}).items():
        if 'type' not in def_details:
            errors.append(f"Definition missing 'type': {def_name}")
        
        if def_details.get('type') == 'object' and 'properties' not in def_details:
            errors.append(f"Object definition missing 'properties': {def_name}")
    
    return len(errors) == 0, errors


def validate_security(swagger_data: Dict) -> Tuple[bool, List[str]]:
    """Validate security definitions."""
    warnings = []
    
    if 'securityDefinitions' not in swagger_data:
        warnings.append("Missing securityDefinitions")
    
    if 'security' not in swagger_data:
        warnings.append("Missing global security requirements")
    
    return True, warnings  # Warnings, not errors


def generate_report(swagger_data: Dict) -> Dict:
    """Generate a comprehensive validation report."""
    report = {
        'valid': True,
        'errors': [],
        'warnings': [],
        'stats': {}
    }
    
    # Required fields
    valid, messages = validate_required_fields(swagger_data)
    if not valid:
        report['valid'] = False
        report['errors'].extend(messages)
    
    # Paths
    valid, messages = validate_paths(swagger_data)
    if not valid:
        report['valid'] = False
    report['errors'].extend([m for m in messages if 'Missing description' in m or 'Missing responses' in m])
    report['warnings'].extend([m for m in messages if m not in report['errors']])
    
    # Parameters
    valid, messages = validate_parameters(swagger_data)
    if not valid:
        report['valid'] = False
        report['errors'].extend(messages)
    
    # Definitions
    valid, messages = validate_definitions(swagger_data)
    if not valid:
        report['valid'] = False
        report['errors'].extend(messages)
    
    # Security
    valid, messages = validate_security(swagger_data)
    report['warnings'].extend(messages)
    
    # Calculate statistics
    total_paths = sum(1 for path, methods in swagger_data.get('paths', {}).items()
                      for method in methods if method != 'parameters' and isinstance(methods[method], dict))
    total_params = sum(len(details.get('parameters', []))
                       for path, methods in swagger_data.get('paths', {}).items()
                       for method, details in methods.items()
                       if method != 'parameters' and isinstance(details, dict))
    
    report['stats'] = {
        'total_paths': total_paths,
        'total_parameters': total_params,
        'total_definitions': len(swagger_data.get('definitions', {})),
        'api_version': swagger_data.get('info', {}).get('version'),
        'api_title': swagger_data.get('info', {}).get('title'),
    }
    
    return report


def print_report(report: Dict):
    """Print validation report."""
    print("=" * 80)
    print("DISPATCHARR API SWAGGER VALIDATION REPORT")
    print("=" * 80)
    
    # API Info
    print("\nAPI Information:")
    print(f"  Title: {report['stats']['api_title']}")
    print(f"  Version: {report['stats']['api_version']}")
    
    # Statistics
    print("\nStatistics:")
    print(f"  Total Endpoints: {report['stats']['total_paths']}")
    print(f"  Total Parameters: {report['stats']['total_parameters']}")
    print(f"  Total Definitions: {report['stats']['total_definitions']}")
    
    # Errors
    if report['errors']:
        print(f"\n❌ ERRORS ({len(report['errors'])}):")
        for error in report['errors'][:10]:
            print(f"  - {error}")
        if len(report['errors']) > 10:
            print(f"  ... and {len(report['errors']) - 10} more")
    else:
        print("\n✓ No errors found")
    
    # Warnings
    if report['warnings']:
        print(f"\n⚠ WARNINGS ({len(report['warnings'])}):")
        for warning in report['warnings'][:10]:
            print(f"  - {warning}")
        if len(report['warnings']) > 10:
            print(f"  ... and {len(report['warnings']) - 10} more")
    
    # Overall status
    print("\n" + "=" * 80)
    if report['valid']:
        print("✓ SWAGGER.JSON IS VALID AND COMPLIANT")
    else:
        print("❌ SWAGGER.JSON HAS VALIDATION ERRORS")
    print("=" * 80)


def main():
    """Main validation function."""
    swagger_file = 'swagger.json'
    
    try:
        with open(swagger_file, 'r') as f:
            swagger_data = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load {swagger_file}: {e}")
        return 1
    
    # Generate and print report
    report = generate_report(swagger_data)
    print_report(report)
    
    return 0 if report['valid'] else 1


if __name__ == '__main__':
    sys.exit(main())
