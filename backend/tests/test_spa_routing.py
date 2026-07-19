#!/usr/bin/env python3
"""
Unit test to verify SPA (Single Page Application) routing support.

This test verifies that Flask routes are configured correctly to support
client-side routing for the React frontend, while still serving API endpoints.
"""

import unittest
import os
import sys
import tempfile
from pathlib import Path

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSPARouting(unittest.TestCase):
    """Test SPA routing configuration in Flask."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directories
        self.temp_dir = tempfile.mkdtemp()
        self.static_dir = Path(self.temp_dir) / 'static'
        self.static_dir.mkdir(parents=True, exist_ok=True)
        
        # Create a test index.html
        index_html = self.static_dir / 'index.html'
        index_html.write_text('<!DOCTYPE html><html><body>Test SPA</body></html>')
        
        # Create a test static file
        test_js = self.static_dir / 'test.js'
        test_js.write_text('console.log("test");')
        
        # Set environment variables
        os.environ['CONFIG_DIR'] = self.temp_dir
        os.environ['TEST_MODE'] = 'true'
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_flask_app_initialization(self):
        """Test that Flask app initializes with correct static path configuration."""
        # Need to import after setting up environment
        from web_api import app
        
        # Verify static_folder is disabled (None) to avoid conflicts with React's /static path
        # The catch-all route handles all static file serving
        self.assertIsNone(app.static_folder, "static_folder should be None to disable Flask's built-in static route")
    
    def test_route_ordering(self):
        """Test that catch-all route is registered last."""
        from web_api import app
        
        routes = list(app.url_map.iter_rules())
        route_endpoints = [rule.endpoint for rule in routes]
        
        # Verify serve_frontend (catch-all) is the last route
        self.assertEqual(route_endpoints[-1], 'serve_frontend')
        
        # Verify API routes come before catch-all
        api_routes = [rule for rule in routes if rule.rule.startswith('/api/')]
        self.assertGreater(len(api_routes), 0, "Should have API routes")
        
        # Get index of catch-all route
        catch_all_index = route_endpoints.index('serve_frontend')
        
        # Verify all API routes come before catch-all
        for api_route in api_routes:
            api_index = route_endpoints.index(api_route.endpoint)
            self.assertLess(api_index, catch_all_index, 
                          f"API route {api_route.endpoint} should come before catch-all")
    
    def test_api_routes_exist(self):
        """Test that all critical API routes are registered."""
        from web_api import app
        
        routes = [rule.rule for rule in app.url_map.iter_rules()]
        
        # Critical API routes that must exist
        required_routes = [
            '/api/health',
            '/api/readiness',
            '/api/automation/status',
            '/api/stream-checker/status',
            '/api/setup-wizard',
        ]
        
        for route in required_routes:
            self.assertIn(route, routes, f"Required route {route} not found")
    
    def test_catch_all_route_pattern(self):
        """Test that catch-all route has correct pattern."""
        from web_api import app
        
        # Find the serve_frontend route
        catch_all = None
        for rule in app.url_map.iter_rules():
            if rule.endpoint == 'serve_frontend':
                catch_all = rule
                break
        
        self.assertIsNotNone(catch_all, "Catch-all route should exist")
        self.assertEqual(catch_all.rule, '/<path:path>')
    
    def test_static_route_pattern(self):
        """Test that Flask's built-in static route is disabled."""
        from web_api import app
        
        # Find the static route
        static_route = None
        for rule in app.url_map.iter_rules():
            if rule.endpoint == 'static':
                static_route = rule
                break
        
        # Flask's built-in static route should not exist since we disabled it
        # to avoid conflicts with React's /static path structure
        self.assertIsNone(static_route, "Flask's built-in static route should not exist")


class TestSPARoutingIntegration(unittest.TestCase):
    """Integration tests for SPA routing with test client."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Set environment variables
        os.environ['CONFIG_DIR'] = tempfile.mkdtemp()
        os.environ['TEST_MODE'] = 'true'
        
        # Import and configure app after environment is set
        from web_api import app
        app.config['TESTING'] = True
        
        self.app = app
        self.client = app.test_client()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        config_dir = os.environ.get('CONFIG_DIR')
        if config_dir and os.path.exists(config_dir):
            shutil.rmtree(config_dir, ignore_errors=True)
    
    def test_api_endpoint_returns_json(self):
        """Test that API endpoints return JSON, not HTML."""
        response = self.client.get('/api/health')
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'application/json')
        
        # Verify it's actually JSON
        import json
        try:
            json.loads(response.data)
        except json.JSONDecodeError:
            self.fail("Response should be valid JSON")
    
    def test_api_routes_do_not_return_html(self):
        """Test that API routes return JSON."""
        api_routes = [
            '/api/health',
            '/api/setup-wizard',
        ]
        
        for route in api_routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                
                # Should be JSON, not HTML
                self.assertEqual(response.content_type, 'application/json',
                               f"API route {route} should return JSON")
    
    def test_catch_all_handles_non_api_routes(self):
        """Test that non-API routes are handled by catch-all."""
        # These routes should be handled by the catch-all route
        # The catch-all serves index.html for SPA client-side routing
        frontend_routes = [
            '/stream-checker',
            '/channels',
            '/settings',
        ]
        
        for route in frontend_routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                
                # The catch-all should serve index.html for SPA routing
                # (React Router handles the actual route on the client side)
                # If index.html exists, we get 200; if not, 404
                self.assertIn(response.status_code, [200, 404],
                            f"Frontend route {route} should return 200 (with index.html) or 404 (without)")
                
                # If 404, it should mention "Frontend not found"
                if response.status_code == 404 and response.content_type == 'application/json':
                    import json
                    data = json.loads(response.data)
                    self.assertIn('error', data)
                    self.assertIn('Frontend not found', data['error'])


    def test_catch_all_blocks_path_traversal(self):
        """Test that path traversal attempts are blocked with 400."""
        # Request with path traversal
        response = self.client.get('/../../secret.txt')
        
        # It should return 400 Bad Request
        self.assertEqual(response.status_code, 400)
        
        import json
        data = json.loads(response.data)
        self.assertIn('error', data)
        self.assertEqual(data['error'], 'Invalid path')


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)
