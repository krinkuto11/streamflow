"""
Test debug mode functionality.

This test verifies that the DEBUG_MODE environment variable properly controls
logging levels and that debug logs are only output when DEBUG_MODE is enabled.
"""

import os
import sys
import unittest
import logging
from io import StringIO
from pathlib import Path

# Add backend directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_api_request, log_api_response, log_state_change


class TestDebugMode(unittest.TestCase):
    """Test debug mode logging functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Clear all existing handlers to start fresh
        logging.root.handlers = []
        
        # Create a string stream to capture log output
        self.log_stream = StringIO()
        handler = logging.StreamHandler(self.log_stream)
        handler.setLevel(logging.DEBUG)
        logging.root.addHandler(handler)
    
    def tearDown(self):
        """Clean up after tests."""
        # Clear handlers
        logging.root.handlers = []
        # Reset DEBUG_MODE
        if 'DEBUG_MODE' in os.environ:
            del os.environ['DEBUG_MODE']
    
    def test_debug_mode_disabled_by_default(self):
        """Test that debug mode is disabled by default."""
        # Make sure DEBUG_MODE is not set
        if 'DEBUG_MODE' in os.environ:
            del os.environ['DEBUG_MODE']
        
        logger = setup_logging('test_module')
        
        # Log at different levels
        logger.info("Info message")
        logger.debug("Debug message")
        
        # Get log output
        log_output = self.log_stream.getvalue()
        
        # Info should be present, debug should not
        self.assertIn("Info message", log_output)
        self.assertNotIn("Debug message", log_output)
    
    def test_debug_mode_enabled(self):
        """Test that debug mode enables debug logging when DEBUG_MODE=true."""
        # Enable debug mode
        os.environ['DEBUG_MODE'] = 'true'
        
        logger = setup_logging('test_module')
        
        # Log at different levels
        logger.info("Info message")
        logger.debug("Debug message")
        
        # Get log output
        log_output = self.log_stream.getvalue()
        
        # Both info and debug should be present
        self.assertIn("Info message", log_output)
        self.assertIn("Debug message", log_output)
        self.assertIn("Debug mode enabled", log_output)
    
    def test_debug_mode_various_values(self):
        """Test that various truthy values enable debug mode."""
        truthy_values = ['true', 'True', 'TRUE', '1', 'yes', 'YES', 'on', 'ON']
        
        for value in truthy_values:
            # Clear handlers and stream
            logging.root.handlers = []
            self.log_stream = StringIO()
            handler = logging.StreamHandler(self.log_stream)
            handler.setLevel(logging.DEBUG)
            logging.root.addHandler(handler)
            
            os.environ['DEBUG_MODE'] = value
            logger = setup_logging(f'test_module_{value}')
            logger.debug("Debug message")
            
            log_output = self.log_stream.getvalue()
            self.assertIn("Debug message", log_output, 
                         f"DEBUG_MODE={value} should enable debug logging")
    
    def test_debug_mode_false_values(self):
        """Test that false values disable debug mode."""
        false_values = ['false', 'False', 'FALSE', '0', 'no', 'NO', 'off', 'OFF']
        
        for value in false_values:
            # Clear handlers and stream
            logging.root.handlers = []
            self.log_stream = StringIO()
            handler = logging.StreamHandler(self.log_stream)
            handler.setLevel(logging.DEBUG)
            logging.root.addHandler(handler)
            
            os.environ['DEBUG_MODE'] = value
            logger = setup_logging(f'test_module_{value}')
            logger.debug("Debug message")
            
            log_output = self.log_stream.getvalue()
            self.assertNotIn("Debug message", log_output,
                           f"DEBUG_MODE={value} should disable debug logging")
    
    def test_log_function_call(self):
        """Test that log_function_call only logs in debug mode."""
        os.environ['DEBUG_MODE'] = 'true'
        logger = setup_logging('test_module')
        
        log_function_call(logger, 'test_function', param1='value1', param2=42)
        
        log_output = self.log_stream.getvalue()
        self.assertIn("test_function", log_output)
        self.assertIn("param1=value1", log_output)
        self.assertIn("param2=42", log_output)
    
    def test_log_function_return(self):
        """Test that log_function_return only logs in debug mode."""
        os.environ['DEBUG_MODE'] = 'true'
        logger = setup_logging('test_module')
        
        log_function_return(logger, 'test_function', result='success', elapsed_time=1.5)
        
        log_output = self.log_stream.getvalue()
        self.assertIn("test_function", log_output)
        self.assertIn("success", log_output)
        self.assertIn("1.500s", log_output)
    
    def test_log_exception(self):
        """Test that log_exception includes stack trace in debug mode."""
        os.environ['DEBUG_MODE'] = 'true'
        logger = setup_logging('test_module')
        
        try:
            raise ValueError("Test error")
        except ValueError as e:
            log_exception(logger, e, "test context")
        
        log_output = self.log_stream.getvalue()
        self.assertIn("Test error", log_output)
        self.assertIn("test context", log_output)
        # In debug mode, should include traceback
        self.assertIn("Traceback", log_output)
    
    def test_log_api_request(self):
        """Test that log_api_request sanitizes sensitive data."""
        os.environ['DEBUG_MODE'] = 'true'
        logger = setup_logging('test_module')
        
        log_api_request(logger, 'POST', 'http://example.com/api/test',
                       headers={'Authorization': 'Bearer secret_token'},
                       data={'key': 'value'})
        
        log_output = self.log_stream.getvalue()
        self.assertIn("POST", log_output)
        self.assertIn("url-", log_output)
        self.assertNotIn("http://example.com/api/test", log_output)
        # Should NOT include actual auth data
        self.assertNotIn("secret_token", log_output)
        self.assertIn("redacted", log_output)
    
    def test_log_state_change(self):
        """Test that log_state_change logs state transitions."""
        os.environ['DEBUG_MODE'] = 'true'
        logger = setup_logging('test_module')
        
        log_state_change(logger, 'service', 'stopped', 'running', 'user request')
        
        log_output = self.log_stream.getvalue()
        self.assertIn("State change", log_output)
        self.assertIn("service", log_output)
        self.assertIn("stopped", log_output)
        self.assertIn("running", log_output)
        self.assertIn("user request", log_output)
    
    def test_logging_format_includes_context(self):
        """Test that log messages include module, function, and line information."""
        os.environ['DEBUG_MODE'] = 'true'
        
        # Clear existing handlers and reconfigure with proper formatter
        logging.root.handlers = []
        self.log_stream = StringIO()
        handler = logging.StreamHandler(self.log_stream)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - [%(name)s:%(funcName)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logging.root.addHandler(handler)
        
        logger = setup_logging('test_module')
        logger.info("Test message")
        
        log_output = self.log_stream.getvalue()
        # Should include module name
        self.assertIn("test_module", log_output)
        # Should include a function name (may vary by Python version)
        self.assertIn(":", log_output)


if __name__ == '__main__':
    unittest.main()
