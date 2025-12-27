#!/usr/bin/env python3
"""
Test mass regex assignment feature and channel name variable substitution.
"""

import re
import unittest


class TestChannelNameSubstitution(unittest.TestCase):
    """Test the channel name variable substitution logic."""
    
    def substitute_channel_variables(self, pattern: str, channel_name: str) -> str:
        """Substitute channel name variables in a regex pattern.
        
        Supports both {CHANNEL_NAME} and CHANNEL_NAME formats.
        """
        escaped_channel_name = re.escape(channel_name)
        # Process {CHANNEL_NAME} first to avoid double replacement
        result = pattern.replace('{CHANNEL_NAME}', escaped_channel_name)
        result = result.replace('CHANNEL_NAME', escaped_channel_name)
        return result
    
    def test_basic_substitution(self):
        """Test basic channel name substitution."""
        pattern = ".*CHANNEL_NAME.*"
        channel_name = "ESPN"
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, ".*ESPN.*")
    
    def test_multiple_substitutions(self):
        """Test pattern with multiple channel name occurrences."""
        pattern = "CHANNEL_NAME.*CHANNEL_NAME"
        channel_name = "CNN"
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, "CNN.*CNN")
    
    def test_special_regex_characters(self):
        """Test that special regex characters in channel names are escaped."""
        pattern = ".*CHANNEL_NAME.*"
        channel_name = "ESPN+"
        result = self.substitute_channel_variables(pattern, channel_name)
        # + should be escaped to \+
        self.assertEqual(result, r".*ESPN\+.*")
        
        # Verify it works as a valid regex
        try:
            re.compile(result)
        except re.error:
            self.fail("Generated pattern should be valid regex")
    
    def test_complex_pattern(self):
        """Test more complex pattern with channel name variable."""
        pattern = r"^(?:USA|UK)\s+CHANNEL_NAME(?:\s+HD)?$"
        channel_name = "Discovery"
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, r"^(?:USA|UK)\s+Discovery(?:\s+HD)?$")
    
    def test_no_variable(self):
        """Test pattern without channel name variable stays unchanged."""
        pattern = ".*News.*"
        channel_name = "CNN"
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, ".*News.*")
    
    def test_matching_with_substitution(self):
        """Test that substituted patterns match correctly."""
        pattern = ".*CHANNEL_NAME.*"
        
        # Test with ESPN
        channel_name = "ESPN"
        substituted = self.substitute_channel_variables(pattern, channel_name)
        regex = re.compile(substituted, re.IGNORECASE)
        
        self.assertTrue(regex.search("USA ESPN HD"))
        self.assertTrue(regex.search("ESPN Sports"))
        self.assertTrue(regex.search("espn"))
        self.assertFalse(regex.search("CNN News"))
        
        # Test with CNN
        channel_name = "CNN"
        substituted = self.substitute_channel_variables(pattern, channel_name)
        regex = re.compile(substituted, re.IGNORECASE)
        
        self.assertTrue(regex.search("CNN International"))
        self.assertTrue(regex.search("USA CNN"))
        self.assertFalse(regex.search("ESPN Sports"))
    
    def test_dots_in_channel_name(self):
        """Test channel names with dots are properly escaped."""
        pattern = ".*CHANNEL_NAME.*"
        channel_name = "ABC.com"
        result = self.substitute_channel_variables(pattern, channel_name)
        # Dots should be escaped to \.
        self.assertIn(r"ABC\.com", result)
    
    def test_curly_brace_format(self):
        """Test that {CHANNEL_NAME} format works."""
        pattern = ".*{CHANNEL_NAME}.*"
        channel_name = "HBO"
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, ".*HBO.*")
    
    def test_curly_brace_with_special_chars(self):
        """Test {CHANNEL_NAME} format with special characters."""
        pattern = ".*{CHANNEL_NAME}.*"
        channel_name = "ESPN+"
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, r".*ESPN\+.*")
    
    def test_backward_compatibility(self):
        """Test that old CHANNEL_NAME format still works."""
        old_pattern = ".*CHANNEL_NAME.*"
        new_pattern = ".*{CHANNEL_NAME}.*"
        channel_name = "Discovery"
        
        old_result = self.substitute_channel_variables(old_pattern, channel_name)
        new_result = self.substitute_channel_variables(new_pattern, channel_name)
        
        # Both formats should produce the same result
        self.assertEqual(old_result, new_result)
        self.assertEqual(old_result, ".*Discovery.*")


class TestBulkAssignmentLogic(unittest.TestCase):
    """Test bulk assignment logic."""
    
    def test_merge_patterns(self):
        """Test that bulk assignment merges patterns correctly."""
        existing_patterns = [".*ESPN.*"]
        new_patterns = [".*CHANNEL_NAME.*"]
        
        # Simulate merge logic
        merged = list(existing_patterns)
        for pattern in new_patterns:
            if pattern not in merged:
                merged.append(pattern)
        
        self.assertEqual(len(merged), 2)
        self.assertIn(".*ESPN.*", merged)
        self.assertIn(".*CHANNEL_NAME.*", merged)
    
    def test_avoid_duplicates(self):
        """Test that duplicate patterns are not added."""
        existing_patterns = [".*ESPN.*", ".*Sports.*"]
        new_patterns = [".*ESPN.*"]  # Duplicate
        
        # Simulate merge logic
        merged = list(existing_patterns)
        for pattern in new_patterns:
            if pattern not in merged:
                merged.append(pattern)
        
        # Should still have only 2 patterns
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged.count(".*ESPN.*"), 1)


if __name__ == '__main__':
    unittest.main()
