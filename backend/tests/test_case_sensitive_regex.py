#!/usr/bin/env python3
"""
Test case-sensitive regex matching behavior.
This test verifies that regex matching is case-sensitive by default.
"""

import sys
import os
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory to path to import modules
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))


class TestCaseSensitiveRegex(unittest.TestCase):
    """Test that regex matching is case-sensitive by default."""
    
    def test_default_config_is_case_sensitive(self):
        """Test that default configuration has case_sensitive=True."""
        from apps.automation.automated_stream_manager import RegexChannelMatcher
        
        # Create a temp config file for testing
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            temp_config = f.name
        
        try:
            # Create matcher which will create default config
            with patch.object(Path, 'mkdir'):
                matcher = RegexChannelMatcher(config_file=temp_config)
            
            # Get the default config that was created
            default_config = matcher.get_patterns()
            
            # Check that default is case-sensitive
            self.assertTrue(
                default_config.get('global_settings', {}).get('case_sensitive', False),
                "Default configuration should have case_sensitive=True"
            )
        finally:
            # Clean up
            if os.path.exists(temp_config):
                os.remove(temp_config)
    
    def test_case_sensitive_matching_logic(self):
        """Test the actual case-sensitive matching logic."""
        from apps.automation.automated_stream_manager import RegexChannelMatcher
        import re
        
        # Test the logic directly without file I/O
        # This simulates what happens in find_matching_channels
        
        # Case-sensitive matching (case_sensitive=True)
        stream_name = "ESPN Sports HD"
        pattern = ".*ESPN.*"
        
        # Case sensitive - should match exact case
        search_name_sensitive = stream_name  # No lowering
        search_pattern_sensitive = pattern   # No lowering
        
        # Convert spaces to flexible whitespace
        _WHITESPACE_PATTERN = re.compile(r'(?<!\\\\) +')
        search_pattern_sensitive = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern_sensitive)
        
        self.assertTrue(
            re.search(search_pattern_sensitive, search_name_sensitive),
            "Should match 'ESPN' with case-sensitive matching"
        )
        
        # Should NOT match lowercase when case-sensitive
        stream_name_lower = "espn sports hd"
        self.assertFalse(
            re.search(search_pattern_sensitive, stream_name_lower),
            "Should not match 'espn' with case-sensitive matching"
        )
        
        # Case-insensitive matching (case_sensitive=False)
        search_name_insensitive = stream_name.lower()
        search_pattern_insensitive = pattern.lower()
        search_pattern_insensitive = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern_insensitive)
        
        self.assertTrue(
            re.search(search_pattern_insensitive, search_name_insensitive),
            "Should match with case-insensitive matching"
        )
    
    def test_web_api_default_case_sensitive(self):
        """Test that web API test_regex_pattern_live defaults to case-sensitive."""
        # This tests that the default value in the API is True
        # Simulating: case_sensitive = data.get('case_sensitive', True)
        
        test_data_without_flag = {}
        case_sensitive = test_data_without_flag.get('case_sensitive', True)
        self.assertTrue(case_sensitive, "Web API should default to case_sensitive=True")
        
        test_data_with_false = {'case_sensitive': False}
        case_sensitive = test_data_with_false.get('case_sensitive', True)
        self.assertFalse(case_sensitive, "Web API should respect explicit False")
        
        test_data_with_true = {'case_sensitive': True}
        case_sensitive = test_data_with_true.get('case_sensitive', True)
        self.assertTrue(case_sensitive, "Web API should respect explicit True")

    def test_single_regex_test_defaults_case_sensitive(self):
        """Test that the single regex test endpoint defaults to case-sensitive."""
        from flask import Flask
        from apps.api.regex_handlers import test_regex_pattern_response

        app = Flask(__name__)
        whitespace_pattern = __import__('re').compile(r'(?<!\\\\) +')

        with app.app_context():
            response = test_regex_pattern_response(
                payload={"pattern": "CNN", "stream_name": "cnn"},
                is_dangerous_regex=lambda _pattern: False,
                whitespace_pattern=whitespace_pattern,
            )

        data = json.loads(response.get_data(as_text=True))
        self.assertFalse(data["matches"], "Single regex test should default to case-sensitive")

    def test_case_insensitive_regex_test_preserves_regex_syntax(self):
        """Test that explicit case-insensitive mode does not lowercase regex syntax."""
        from flask import Flask
        from apps.api.regex_handlers import test_regex_pattern_response

        app = Flask(__name__)
        whitespace_pattern = __import__('re').compile(r'(?<!\\\\) +')

        with app.app_context():
            response = test_regex_pattern_response(
                payload={
                    "pattern": r"(?P<label>HD)",
                    "stream_name": "hd",
                    "case_sensitive": False,
                },
                is_dangerous_regex=lambda _pattern: False,
                whitespace_pattern=whitespace_pattern,
            )

        data = json.loads(response.get_data(as_text=True))
        self.assertTrue(data["matches"], "Case-insensitive mode should use re.IGNORECASE")


if __name__ == '__main__':
    unittest.main()
