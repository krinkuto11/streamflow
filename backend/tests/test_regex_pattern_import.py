
#!/usr/bin/env python3
"""
Unit tests for regex pattern import functionality.

This module tests:
- JSON import endpoint validation
- Pattern structure validation
- Regex validation during import
- Error handling for invalid JSON
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.automation.automated_stream_manager import RegexChannelMatcher


class TestRegexPatternImport(unittest.TestCase):
    """Test regex pattern import functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_valid_pattern_import(self):
        """Test importing valid patterns."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            matcher = RegexChannelMatcher()
            
            # Create valid pattern data
            patterns = {
                "patterns": {
                    "1": {
                        "name": "CNN",
                        "regex": [".*CNN.*"],
                        "enabled": True
                    },
                    "2": {
                        "name": "ESPN",
                        "regex": [".*ESPN.*"],
                        "enabled": True
                    }
                },
                "global_settings": {
                    "case_sensitive": False
                }
            }
            
            # Save patterns
            matcher._save_patterns(patterns)
            
            # Reload and verify
            matcher.reload_patterns()
            loaded_patterns = matcher.get_patterns()
            
            self.assertIn("patterns", loaded_patterns)
            self.assertEqual(len(loaded_patterns["patterns"]), 2)
            self.assertIn("1", loaded_patterns["patterns"])
            self.assertIn("2", loaded_patterns["patterns"])
    
    def test_invalid_regex_pattern_validation(self):
        """Test that invalid regex patterns are rejected."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            matcher = RegexChannelMatcher()
            
            # Test with invalid regex
            invalid_patterns = ["[invalid("]
            is_valid, error_msg = matcher.validate_regex_patterns(invalid_patterns)
            
            self.assertFalse(is_valid)
            self.assertIsNotNone(error_msg)
            self.assertIn("Invalid regex pattern", error_msg)
    
    def test_empty_pattern_list_validation(self):
        """Test that empty pattern lists are rejected."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            matcher = RegexChannelMatcher()
            
            # Test with empty list
            is_valid, error_msg = matcher.validate_regex_patterns([])
            
            self.assertFalse(is_valid)
            self.assertIn("At least one regex pattern is required", error_msg)
    
    def test_import_overwrites_existing_patterns(self):
        """Test that importing patterns overwrites existing ones."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            matcher = RegexChannelMatcher()
            
            # Add initial pattern
            matcher.add_channel_pattern("1", "Initial", [".*Initial.*"])
            
            # Import new patterns
            new_patterns = {
                "patterns": {
                    "1": {
                        "name": "Updated",
                        "regex": [".*Updated.*"],
                        "enabled": True
                    }
                },
                "global_settings": {
                    "case_sensitive": False
                }
            }
            
            matcher._save_patterns(new_patterns)
            matcher.reload_patterns()
            
            loaded_patterns = matcher.get_patterns()
            self.assertEqual(loaded_patterns["patterns"]["1"]["name"], "Updated")
    
    def test_pattern_validation_with_special_characters(self):
        """Test validation of patterns with special regex characters."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            matcher = RegexChannelMatcher()
            
            # Valid patterns with special characters
            valid_patterns = [
                ".*CNN.*",
                "^News.*",
                ".*Sports$",
                "ESPN|Fox Sports",
                "\\d+ News"
            ]
            
            is_valid, error_msg = matcher.validate_regex_patterns(valid_patterns)
            self.assertTrue(is_valid)
            self.assertIsNone(error_msg)
    
    def test_pattern_matching_after_import(self):
        """Test that patterns work correctly after import."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            matcher = RegexChannelMatcher()
            
            # Import patterns
            patterns = {
                "patterns": {
                    "1": {
                        "name": "News",
                        "regex": [".*CNN.*", ".*BBC.*"],
                        "enabled": True
                    }
                },
                "global_settings": {
                    "case_sensitive": False
                }
            }
            
            matcher._save_patterns(patterns)
            matcher.reload_patterns()
            
            # Test matching
            matches = matcher.match_stream_to_channels("CNN International")
            self.assertIn("1", matches)
            
            matches = matcher.match_stream_to_channels("BBC World News")
            self.assertIn("1", matches)
            
            matches = matcher.match_stream_to_channels("ESPN Sports")
            self.assertNotIn("1", matches)



    def test_import_does_not_duplicate_patterns_on_repeated_import(self):
        """Regression test: repeated non-merge imports must not accumulate pattern rows.

        Root cause: import_channel_regex_configs_from_json used SQLAlchemy bulk
        query.delete() which bypasses ORM cascade. SQLite does not enforce foreign
        key CASCADE without PRAGMA foreign_keys = ON, so ChannelRegexPattern rows
        survived the ChannelRegexConfig wipe and became orphans. A second import
        then added new pattern rows on top, causing duplicates. The fix explicitly
        deletes ChannelRegexPattern rows before ChannelRegexConfig rows.
        """
        from apps.database.manager import get_db_manager
        from apps.database.models import ChannelRegexPattern
        from apps.database.connection import get_session

        db = get_db_manager()

        payload = {
            "patterns": {
                "1": {
                    "name": "Fox News",
                    "enabled": True,
                    "match_by_tvg_id": False,
                    "regex_patterns": [
                        {"pattern": "(?i)fox\\s+news", "m3u_accounts": None, "priority": 0}
                    ],
                },
                "2": {
                    "name": "ESPN",
                    "enabled": True,
                    "match_by_tvg_id": False,
                    "regex_patterns": [
                        {"pattern": "(?i)ESPN", "m3u_accounts": None, "priority": 0}
                    ],
                },
            }
        }

        # First import
        imported, errors = db.import_channel_regex_configs_from_json(payload, merge=False)
        self.assertEqual(errors, [], f"First import had errors: {errors}")
        self.assertEqual(imported, 2)

        # Second import — must fully replace, not append
        imported, errors = db.import_channel_regex_configs_from_json(payload, merge=False)
        self.assertEqual(errors, [], f"Second import had errors: {errors}")
        self.assertEqual(imported, 2)

        # Verify pattern counts — each channel must have exactly 1 pattern row
        session = get_session()
        try:
            for channel_id in ("1", "2"):
                count = (
                    session.query(ChannelRegexPattern)
                    .filter(ChannelRegexPattern.channel_id == channel_id)
                    .count()
                )
                self.assertEqual(
                    count,
                    1,
                    f"Channel {channel_id} has {count} pattern rows after two imports "
                    f"(expected 1) — duplicate pattern bug has regressed",
                )
        finally:
            session.close()

        # Also verify no orphaned pattern rows exist
        session = get_session()
        try:
            from apps.database.models import ChannelRegexConfig
            config_ids = {
                row.channel_id
                for row in session.query(ChannelRegexConfig).all()
            }
            orphan_count = (
                session.query(ChannelRegexPattern)
                .filter(ChannelRegexPattern.channel_id.notin_(config_ids))
                .count()
            )
            self.assertEqual(
                orphan_count,
                0,
                f"Found {orphan_count} orphaned ChannelRegexPattern rows after import",
            )
        finally:
            session.close()

if __name__ == '__main__':
    unittest.main()
