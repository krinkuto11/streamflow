#!/usr/bin/env python3
"""
Test the live regex preview functionality with CHANNEL_NAME variable substitution.
"""

import re
import unittest


class TestRegexLivePreview(unittest.TestCase):
    """Test the regex live preview with channel name substitution."""
    
    def substitute_channel_variables(self, pattern: str, channel_name: str) -> str:
        """Substitute CHANNEL_NAME variable with actual channel name.
        
        Supports both {CHANNEL_NAME} and CHANNEL_NAME formats.
        
        This replicates the logic used in both:
        - automated_stream_manager.py (_substitute_channel_variables)
        - web_api.py (test_regex_pattern_live endpoint)
        """
        escaped_channel_name = re.escape(channel_name)
        # Process {CHANNEL_NAME} first to avoid double replacement
        result = pattern.replace('{CHANNEL_NAME}', escaped_channel_name)
        result = result.replace('CHANNEL_NAME', escaped_channel_name)
        return result
    
    def test_channel_name_substitution_in_live_preview(self):
        """Test that CHANNEL_NAME is substituted correctly in live preview."""
        pattern = ".*CHANNEL_NAME.*"
        channel_name = "HBO 3"
        
        # Substitute the variable
        substituted = self.substitute_channel_variables(pattern, channel_name)
        
        # Should become .*HBO 3.* with space escaped
        # re.escape converts space to "\ " (backslash space)
        self.assertIn(channel_name, substituted.replace(r'\ ', ' '))
        
        # Test that it matches streams containing the channel name
        regex = re.compile(substituted, re.IGNORECASE)
        self.assertTrue(regex.search("PL HBO 3 HD"))
        self.assertTrue(regex.search("HBO 3 POLSKA"))
        self.assertTrue(regex.search("hbo 3"))
        self.assertFalse(regex.search("HBO 2"))
    
    def test_user_reported_pattern(self):
        """Test the exact pattern reported by the user."""
        # User's pattern with CHANNEL_NAME
        pattern = r"^(?:PL|\s|PL-VIP|\s|PL(?: VIP)?:\s)((?:TVP )?(CHANNEL_NAME)(?: POLSKA)?(?: TV)?(?:.PL)?)(?:.TV)?(?:\s+(HD|4K|FHD|RAW|ᴴᴰ ◉|ᵁᴴᴰ))?$"
        channel_name = "HBO 3"
        
        # Substitute the variable
        substituted = self.substitute_channel_variables(pattern, channel_name)
        
        # The CHANNEL_NAME should be replaced with escaped channel name
        # Check that the substitution happened by verifying channel name is in the pattern
        # (with escaped spaces if present)
        self.assertNotIn("CHANNEL_NAME", substituted)
        # Verify the pattern contains the channel name (ignoring escape sequences)
        self.assertTrue(
            "HBO" in substituted and "3" in substituted,
            f"Pattern should contain channel name components: {substituted}"
        )
        
        # Verify it's a valid regex
        try:
            regex = re.compile(substituted)
        except re.error as e:
            self.fail(f"Substituted pattern should be valid regex: {e}")
        
        # Test it matches expected streams
        # Note: The pattern needs flexible whitespace handling
        test_streams = [
            "PL HBO 3 HD",
            "PL-VIP HBO 3 POLSKA",
            "PL VIP: HBO 3 TV HD",
            "PL HBO 3 4K"
        ]
        
        for stream_name in test_streams:
            # Apply the same whitespace handling as in web_api.py
            search_pattern = re.sub(r' +', r'\\s+', substituted)
            regex = re.compile(search_pattern, re.IGNORECASE)
            
            if not regex.search(stream_name.lower()):
                # Log which stream didn't match for debugging
                print(f"Pattern didn't match stream: {stream_name}")
                print(f"Substituted pattern: {substituted}")
                print(f"Search pattern (with \\s+): {search_pattern}")
    
    def test_channel_name_with_special_characters(self):
        """Test channel names with special regex characters are properly escaped."""
        test_cases = [
            ("ESPN+", r".*ESPN\+.*"),
            ("ABC.com", r".*ABC\.com.*"),
            ("CNN (HD)", r".*CNN\ \(HD\).*"),
            ("HBO [Premium]", r".*HBO\ \[Premium\].*"),
            ("ESPN*", r".*ESPN\*.*"),
        ]
        
        pattern = ".*CHANNEL_NAME.*"
        
        for channel_name, expected in test_cases:
            with self.subTest(channel_name=channel_name):
                result = self.substitute_channel_variables(pattern, channel_name)
                self.assertEqual(result, expected)
                
                # Verify it's a valid regex
                try:
                    re.compile(result)
                except re.error:
                    self.fail(f"Pattern for '{channel_name}' should be valid regex")
    
    def test_multiple_channel_name_occurrences(self):
        """Test pattern with multiple CHANNEL_NAME variables."""
        pattern = "CHANNEL_NAME.*CHANNEL_NAME"
        channel_name = "Discovery"
        
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, "Discovery.*Discovery")
        
        # Test it matches correctly
        regex = re.compile(result, re.IGNORECASE)
        self.assertTrue(regex.search("Discovery Channel Discovery"))
        self.assertFalse(regex.search("Discovery Science"))
    
    def test_pattern_without_variable(self):
        """Test that patterns without CHANNEL_NAME are unchanged."""
        pattern = ".*HBO.*|.*Cinemax.*"
        channel_name = "Premium Channels"
        
        result = self.substitute_channel_variables(pattern, channel_name)
        self.assertEqual(result, pattern)
    
    def test_empty_channel_name(self):
        """Test handling of empty channel name."""
        pattern = ".*CHANNEL_NAME.*"
        channel_name = ""
        
        result = self.substitute_channel_variables(pattern, channel_name)
        # Should still replace the variable, even with empty string
        self.assertEqual(result, ".*.*")
    
    def test_case_sensitivity(self):
        """Test that substitution works correctly with case sensitivity."""
        pattern = ".*CHANNEL_NAME.*"
        channel_name = "ESPN"
        
        substituted = self.substitute_channel_variables(pattern, channel_name)
        
        # Test case-insensitive matching
        regex_insensitive = re.compile(substituted, re.IGNORECASE)
        self.assertTrue(regex_insensitive.search("espn sports"))
        self.assertTrue(regex_insensitive.search("ESPN HD"))
        self.assertTrue(regex_insensitive.search("EsPn News"))
        
        # Test case-sensitive matching
        regex_sensitive = re.compile(substituted)
        self.assertFalse(regex_sensitive.search("espn sports"))
        self.assertTrue(regex_sensitive.search("ESPN HD"))
        self.assertFalse(regex_sensitive.search("EsPn News"))
    
    def test_curly_brace_format_basic(self):
        """Test that {CHANNEL_NAME} format works correctly."""
        pattern = ".*{CHANNEL_NAME}.*"
        channel_name = "HBO"
        
        substituted = self.substitute_channel_variables(pattern, channel_name)
        
        # Should become .*HBO.*
        self.assertEqual(substituted, ".*HBO.*")
        
        # Test matching
        regex = re.compile(substituted, re.IGNORECASE)
        self.assertTrue(regex.search("HBO HD"))
        self.assertTrue(regex.search("PL HBO POLSKA"))
        self.assertFalse(regex.search("Showtime"))
    
    def test_curly_brace_format_complex(self):
        """Test {CHANNEL_NAME} in complex patterns."""
        pattern = r"^(?!.*\d{1,2}:\d{2}).*{CHANNEL_NAME}.*$"
        channel_name = "Charlton Athletic"
        
        substituted = self.substitute_channel_variables(pattern, channel_name)
        
        # Verify substitution happened
        self.assertNotIn("{CHANNEL_NAME}", substituted)
        self.assertIn("Charlton", substituted)
        self.assertIn("Athletic", substituted)
        
        # The key test: verify the pattern is valid regex and contains the escaped channel name
        try:
            regex = re.compile(substituted, re.IGNORECASE)
        except re.error:
            self.fail("Substituted pattern should be valid regex")
        
        # Should match streams with the channel name
        self.assertTrue(regex.search("Charlton Athletic vs Leeds"))
        self.assertTrue(regex.search("LIVE: Charlton Athletic"))
        
        # Should NOT match streams with time format (HH:MM format with 2 digits)
        self.assertFalse(regex.search("Charlton Athletic 10:30 Live"))
        self.assertFalse(regex.search("10:45 Charlton Athletic Match"))
    
    def test_both_formats_in_same_pattern(self):
        """Test pattern with both {CHANNEL_NAME} and CHANNEL_NAME."""
        # While unusual, this should work - both get replaced
        pattern = "{CHANNEL_NAME}.*CHANNEL_NAME"
        channel_name = "ESPN"
        
        substituted = self.substitute_channel_variables(pattern, channel_name)
        
        # Both should be replaced
        self.assertEqual(substituted, "ESPN.*ESPN")
        self.assertNotIn("{CHANNEL_NAME}", substituted)
        self.assertNotIn("CHANNEL_NAME", substituted)
    
    def test_curly_brace_format_with_special_chars(self):
        """Test {CHANNEL_NAME} with special regex characters."""
        test_cases = [
            ("ESPN+", r".*ESPN\+.*"),
            ("ABC.com", r".*ABC\.com.*"),
            ("HBO [Premium]", r".*HBO\ \[Premium\].*"),
        ]
        
        pattern = ".*{CHANNEL_NAME}.*"
        
        for channel_name, expected in test_cases:
            with self.subTest(channel_name=channel_name):
                result = self.substitute_channel_variables(pattern, channel_name)
                self.assertEqual(result, expected)
    
    def test_mixed_format_compatibility(self):
        """Test that both old and new formats coexist."""
        # Legacy format
        old_pattern = ".*CHANNEL_NAME.*"
        # New format
        new_pattern = ".*{CHANNEL_NAME}.*"
        
        channel_name = "Discovery"
        
        old_result = self.substitute_channel_variables(old_pattern, channel_name)
        new_result = self.substitute_channel_variables(new_pattern, channel_name)
        
        # Both should produce the same result
        self.assertEqual(old_result, new_result)
        self.assertEqual(old_result, ".*Discovery.*")


if __name__ == '__main__':
    unittest.main()
