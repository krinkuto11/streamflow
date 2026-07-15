"""
Test to verify that M3U free profile search/replace patterns are applied during stream checking.

This test validates that when using an M3U account's free profile with search_pattern and
replace_pattern configured, the stream URL is correctly transformed before being passed to
ffmpeg for stream analysis.
"""

import unittest
import sys
import os
import re

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestM3UProfileURLTransformation(unittest.TestCase):
    """Test M3U profile URL transformation during stream checking."""
    
    def test_apply_profile_url_transformation_basic(self):
        """Test basic URL transformation with search/replace pattern."""
        # Test the transformation logic directly without importing UDI
        original_url = 'http://example.com:8080/live/stream123/index.m3u8'
        search_pattern = r':8080/'
        replace_pattern = ':8888/'
        
        # Apply transformation
        transformed_url = re.sub(search_pattern, replace_pattern, original_url)
        
        # Verify transformation
        self.assertEqual(transformed_url, 'http://example.com:8888/live/stream123/index.m3u8')
        self.assertNotEqual(transformed_url, original_url)
    
    def test_apply_profile_url_transformation_complex_pattern(self):
        """Test complex URL transformation pattern."""
        original_url = 'http://premium.example.com/live/user123/pass456/stream.m3u8'
        search_pattern = r'/user123/pass456/'
        replace_pattern = '/freeuser/freepass/'
        
        transformed_url = re.sub(search_pattern, replace_pattern, original_url)
        
        self.assertEqual(transformed_url, 'http://premium.example.com/live/freeuser/freepass/stream.m3u8')
    
    def test_apply_profile_url_transformation_no_match(self):
        """Test URL transformation when pattern doesn't match."""
        original_url = 'http://example.com/stream.m3u8'
        search_pattern = r':9999/'
        replace_pattern = ':8888/'
        
        transformed_url = re.sub(search_pattern, replace_pattern, original_url)
        
        # Should return original URL when pattern doesn't match
        self.assertEqual(transformed_url, original_url)
    
    def test_transformation_with_multiple_replacements(self):
        """Test URL transformation with pattern that matches multiple times."""
        original_url = 'http://server1.example.com/server1/stream.m3u8'
        search_pattern = r'server1'
        replace_pattern = 'server2'
        
        transformed_url = re.sub(search_pattern, replace_pattern, original_url)
        
        # Both occurrences should be replaced
        self.assertEqual(transformed_url, 'http://server2.example.com/server2/stream.m3u8')
        self.assertNotIn('server1', transformed_url)
    
    def test_invalid_regex_handling(self):
        """Test that invalid regex patterns are handled gracefully."""
        original_url = 'http://example.com/stream.m3u8'
        search_pattern = r'[invalid(regex'  # Invalid regex
        replace_pattern = ':8888/'
        
        # Should raise exception
        with self.assertRaises(re.error):
            re.sub(search_pattern, replace_pattern, original_url)
    
    def test_url_transformation_edge_cases(self):
        """Test URL transformation edge cases."""
        # Empty URL
        self.assertEqual(re.sub(r'test', 'replacement', ''), '')
        
        # URL with special characters
        url_with_special = 'http://example.com/stream?token=abc&key=123'
        transformed = re.sub(r'token=\w+', 'token=xyz', url_with_special)
        self.assertEqual(transformed, 'http://example.com/stream?token=xyz&key=123')
        
        # URL with regex metacharacters in replacement
        original = 'http://example.com/path/to/stream'
        # Replace 'path/to' with 'new/path'
        transformed = re.sub(r'path/to', 'new/path', original)
        self.assertEqual(transformed, 'http://example.com/new/path/stream')

    def test_explicit_alternate_profile_resolves_changed_credential_url(self):
        """The production resolver binds an alternate profile to its own URL."""
        from apps.udi.manager import UDIManager

        manager = object.__new__(UDIManager)
        stream = {
            'id': 101,
            'url': 'http://provider.test/live/main-user/main-token/101.ts',
        }
        profile = {
            'id': 11,
            'is_default': False,
            'search_pattern': r'/main-user/main-token/',
            'replace_pattern': '/second-user/second-token/',
        }

        eligible, resolved_url, reason = manager.resolve_profile_stream_url(stream, profile)

        self.assertTrue(eligible)
        self.assertEqual(reason, 'profile_url_transformed')
        self.assertEqual(
            resolved_url,
            'http://provider.test/live/second-user/second-token/101.ts',
        )

    def test_live_style_default_noop_rewrite_remains_eligible(self):
        """Dispatcharr default profiles may explicitly rewrite to the stored URL."""
        from apps.udi.manager import UDIManager

        manager = object.__new__(UDIManager)
        stream = {'id': 101, 'url': 'http://provider.test/live/main/main/101.ts'}
        profile = {
            'id': 10,
            'is_default': True,
            'search_pattern': r'/main/main/',
            'replace_pattern': '/main/main/',
        }

        eligible, resolved_url, reason = manager.resolve_profile_stream_url(stream, profile)

        self.assertTrue(eligible)
        self.assertEqual(resolved_url, stream['url'])
        self.assertEqual(reason, 'default_profile_url')

    def test_nondefault_missing_or_nonmatching_rewrite_fails_closed(self):
        """An alternate slot can never silently probe the stored main URL."""
        from apps.udi.manager import UDIManager

        manager = object.__new__(UDIManager)
        stream = {'id': 101, 'url': 'http://provider.test/live/main/main/101.ts'}
        profiles = [
            {
                'id': 11,
                'is_default': False,
                'search_pattern': None,
                'replace_pattern': None,
            },
            {
                'id': 12,
                'is_default': False,
                'search_pattern': r'^http://another-provider\.test/',
                'replace_pattern': 'http://provider.test/live/other/other/',
            },
        ]

        outcomes = [manager.resolve_profile_stream_url(stream, profile) for profile in profiles]

        self.assertTrue(all(not eligible for eligible, _url, _reason in outcomes))
        self.assertTrue(all(url == '' for _eligible, url, _reason in outcomes))

    def test_invalid_regex_log_does_not_expose_pattern_or_url(self):
        """Configuration failures are observable without leaking credentials."""
        from apps.udi.manager import UDIManager

        manager = object.__new__(UDIManager)
        stream = {
            'id': 101,
            'url': 'http://provider.test/live/private-user/private-token/101.ts',
        }
        profile = {
            'id': 11,
            'is_default': False,
            'search_pattern': r'(private-user/private-token',
            'replace_pattern': '/other-user/other-token/',
        }

        with self.assertLogs('apps.udi.manager', level='ERROR') as captured:
            eligible, resolved_url, reason = manager.resolve_profile_stream_url(stream, profile)

        log_output = '\n'.join(captured.output)
        self.assertFalse(eligible)
        self.assertEqual(resolved_url, '')
        self.assertEqual(reason, 'invalid_profile_url_regex')
        self.assertNotIn('private-user', log_output)
        self.assertNotIn('private-token', log_output)
        self.assertNotIn(stream['url'], log_output)


if __name__ == '__main__':
    unittest.main()
