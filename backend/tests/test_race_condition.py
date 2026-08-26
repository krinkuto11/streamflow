
import unittest
import threading
import sys
import os
import time
import random
import tempfile
from pathlib import Path

# Add backend to path
sys.path.append(os.path.abspath('backend'))

# Mock dotenv before importing backend modules
from unittest.mock import MagicMock
sys.modules['dotenv'] = MagicMock()

from apps.automation.automated_stream_manager import RegexChannelMatcher

class TestRaceCondition(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_file = Path(self.temp_dir.name) / 'test_race_config.json'
        # Initial config
        self.channel_id = '100'
        self.initial_config = {
            "patterns": {
                self.channel_id: {
                    "name": "Race Channel",
                    "regex_patterns": [],
                    "enabled": True
                }
            }
        }
        # Create matcher
        self.matcher = RegexChannelMatcher(config_file=self.config_file)
        # Reset state
        self.matcher.channel_patterns = self.initial_config
        self.matcher._save_patterns(self.initial_config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_bulk_adds(self):
        """Simulate concurrent bulk add requests to the same channel"""
        
        # We will spawn N threads, each adding a unique pattern
        num_threads = 20
        threads = []
        
        # This function simulates the logic inside add_bulk_regex_patterns
        def simulate_request(pattern_to_add):
            # Sleep a tiny bit to randomize entry time
            time.sleep(random.random() * 0.01)
            
            # --- CRITICAL SECTION START (from web_api.py) ---
            with self.matcher.lock:
                # 1. READ
                patterns = self.matcher.get_patterns()
                existing_pattern_data = patterns.get('patterns', {}).get(self.channel_id, {})
                existing_regex_patterns = existing_pattern_data.get('regex_patterns', [])
                
                # Normalize existing
                normalized_existing = []
                for p in existing_regex_patterns:
                    if isinstance(p, dict):
                        normalized_existing.append(p)
                
                # Normalize new
                normalized_new = [{"pattern": pattern_to_add, "m3u_accounts": None, "priority": 0}]
                
                # Merge
                merged = list(normalized_existing)
                existing_strings = {p['pattern'] for p in normalized_existing}
                for new_p in normalized_new:
                    if new_p['pattern'] not in existing_strings:
                        merged.append(new_p)
                
                # Simulate some processing time inside the lock to increase contention
                time.sleep(0.001) 
                
                # 2. WRITE
                self.matcher.add_channel_pattern(
                    self.channel_id,
                    "Race Channel",
                    merged,
                    enabled=True,
                    silent=True
                )
            # --- CRITICAL SECTION END ---

        # Create and start threads
        for i in range(num_threads):
            t = threading.Thread(target=simulate_request, args=(f"pattern_{i}",))
            threads.append(t)
            t.start()
            
        # Wait for all to finish
        for t in threads:
            t.join()
            
        # Verify result
        final_patterns = self.matcher.get_patterns()
        channel_patterns = final_patterns['patterns'][self.channel_id]['regex_patterns']
        
        self.assertEqual(len(channel_patterns), num_threads, 
                         f"Expected {num_threads} patterns, but found {len(channel_patterns)}. "
                         "Race condition likely occurred!"
        )
        
        # Verify all patterns are present
        found_patterns = {p['pattern'] for p in channel_patterns}
        for i in range(num_threads):
            self.assertIn(f"pattern_{i}", found_patterns)

if __name__ == '__main__':
    unittest.main()
