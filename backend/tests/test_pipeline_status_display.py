"""
Test that pipeline_mode is included in stream checker status response.

This ensures that the stream checker page can display the correct pipeline mode.
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from stream_checker_service import StreamCheckerService


class TestPipelineStatusDisplay(unittest.TestCase):
    """Test pipeline mode display in status response."""
    
    def setUp(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_status_includes_pipeline_mode(self):
        """Test that get_status() includes pipeline_mode in config."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            # Create service with a specific pipeline mode
            service = StreamCheckerService()
            service.config.update({'pipeline_mode': 'pipeline_2_5'})
            
            # Get status
            status = service.get_status()
            
            # Verify pipeline_mode is in the response
            self.assertIn('config', status)
            self.assertIn('pipeline_mode', status['config'])
            self.assertEqual(status['config']['pipeline_mode'], 'pipeline_2_5')
    
    def test_status_includes_all_pipeline_modes(self):
        """Test that status correctly returns different pipeline modes."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            pipeline_modes = ['pipeline_1', 'pipeline_1_5', 'pipeline_2', 'pipeline_2_5', 'pipeline_3', 'disabled']
            
            for mode in pipeline_modes:
                service.config.update({'pipeline_mode': mode})
                status = service.get_status()
                self.assertEqual(
                    status['config']['pipeline_mode'], 
                    mode,
                    f"Status should include pipeline_mode={mode}"
                )
    
    def test_status_other_config_fields_still_present(self):
        """Test that adding pipeline_mode doesn't remove other config fields."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            status = service.get_status()
            
            # Verify all expected config fields are present
            self.assertIn('config', status)
            config = status['config']
            
            self.assertIn('pipeline_mode', config)
            self.assertIn('check_interval', config)
            self.assertIn('global_check_schedule', config)
            self.assertIn('queue_settings', config)


if __name__ == '__main__':
    unittest.main()
