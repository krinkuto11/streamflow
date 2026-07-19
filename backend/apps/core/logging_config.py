#!/usr/bin/env python3
"""
Centralized logging configuration for StreamFlow.

This module provides a unified logging setup that respects the DEBUG_MODE
environment variable and can be imported by all other modules.
"""

import logging
import os
import sys
import inspect
from typing import Optional, Callable, Any
from functools import wraps

from apps.core.log_sanitizer import scrub_secrets, url_ref


class HTTPLogFilter(logging.Filter):
    """Filter out HTTP-related log messages."""
    
    def filter(self, record):
        # Exclude messages containing HTTP request/response indicators
        message = record.getMessage().lower()
        http_indicators = [
            'http request',
            'http response',
            'status code',
            'get /',
            'post /',
            'put /',
            'delete /',
            'patch /',
            '" with',
            '- - [',  # Common HTTP access log format
            'werkzeug',
        ]
        return not any(indicator in message for indicator in http_indicators)


class SensitiveDataFilter(logging.Filter):
    """Redact URLs and common secret forms from every formatted log record."""

    def filter(self, record):
        record.msg = scrub_secrets(record.getMessage())
        record.args = ()
        return True


def _ensure_root_filters() -> None:
    for handler in logging.root.handlers:
        if not any(isinstance(item, HTTPLogFilter) for item in handler.filters):
            handler.addFilter(HTTPLogFilter())
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())


def setup_logging(module_name: Optional[str] = None) -> logging.Logger:
    """
    Configure logging with DEBUG_MODE support.
    
    Args:
        module_name: Name of the module for the logger. If None, returns root logger.
        
    Returns:
        logging.Logger: Configured logger instance.
    """
    # Get DEBUG_MODE from environment (default: false)
    debug_mode = os.getenv('DEBUG_MODE', 'false').lower() in ('true', '1', 'yes', 'on')
    
    # Set logging level based on DEBUG_MODE
    log_level = logging.DEBUG if debug_mode else logging.INFO
    
    # Configure root logger if not already configured
    if not logging.root.handlers:
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - [%(name)s:%(funcName)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            stream=sys.stdout
        )
        
    else:
        # Update level if logger already exists
        logging.root.setLevel(log_level)
        for handler in logging.root.handlers:
            handler.setLevel(log_level)

    _ensure_root_filters()
    
    # Get logger for the specific module
    logger = logging.getLogger(module_name) if module_name else logging.root
    logger.setLevel(log_level)
    
    if debug_mode:
        logger.debug(f"Debug mode enabled for {module_name or 'root'}")
    
    return logger


def log_function_call(func_or_logger=None, func_name: Optional[str] = None, **kwargs):
    """
    Log a function call with its parameters (only in debug mode).
    
    Can be used in two ways:
    1. As a decorator: @log_function_call
    2. As a function: log_function_call(logger, 'func_name', param1=val1, ...)
    
    Args:
        func_or_logger: Either a function (when used as decorator) or a logger instance
        func_name: Name of the function being called (only when used as function)
        **kwargs: Function parameters to log (only when used as function)
    
    Returns:
        When used as decorator: Returns a decorator function
        When used as function: Returns None
    """
    # Check if being used as a decorator
    # Must be a function (not just callable) and func_name must be None
    # Also check that it's not a Logger instance
    if (inspect.isfunction(func_or_logger) and 
        func_name is None and 
        not isinstance(func_or_logger, logging.Logger)):
        # Being used as @log_function_call
        func = func_or_logger
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get logger from the module where the function is defined
            module_logger = logging.getLogger(func.__module__)
            
            if module_logger.isEnabledFor(logging.DEBUG):
                # Log function call with parameters
                param_strs = []
                
                # Get function signature to properly handle parameters
                try:
                    sig = inspect.signature(func)
                    bound_args = sig.bind_partial(*args, **kwargs)
                    bound_args.apply_defaults()
                    
                    # Log parameters, skipping 'self' and 'cls'
                    for param_name, param_value in bound_args.arguments.items():
                        if param_name not in ('self', 'cls'):
                            param_strs.append(f"{param_name}={param_value}")
                except (ValueError, TypeError):
                    # Fallback: just log positional and keyword args
                    if args:
                        param_strs.extend(str(arg) for arg in args)
                    if kwargs:
                        param_strs.extend(f"{k}={v}" for k, v in kwargs.items() if v is not None)
                
                params = ', '.join(param_strs)
                module_logger.debug(f"→ {func.__name__}({params})")
            
            # Call the original function
            return func(*args, **kwargs)
        
        return wrapper
    else:
        # Being used as log_function_call(logger, 'func_name', ...)
        logger = func_or_logger
        if logger and hasattr(logger, 'isEnabledFor') and logger.isEnabledFor(logging.DEBUG):
            params = ', '.join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
            logger.debug(f"→ {func_name}({params})")


def log_function_return(logger: logging.Logger, func_name: str, result=None, elapsed_time: Optional[float] = None):
    """
    Log a function return with its result (only in debug mode).
    
    Args:
        logger: Logger instance to use
        func_name: Name of the function returning
        result: Return value to log (optional)
        elapsed_time: Execution time in seconds (optional)
    """
    if logger.isEnabledFor(logging.DEBUG):
        msg = f"← {func_name}"
        if result is not None:
            # Truncate long results
            result_str = str(result)
            if len(result_str) > 100:
                result_str = result_str[:100] + "..."
            msg += f" → {result_str}"
        if elapsed_time is not None:
            msg += f" ({elapsed_time:.3f}s)"
        logger.debug(msg)


def log_exception(logger: logging.Logger, exc: Exception, context: str = ""):
    """
    Log an exception with context and stack trace in debug mode.
    
    Args:
        logger: Logger instance to use
        exc: Exception to log
        context: Additional context about where/why the exception occurred
    """
    msg = f"Exception in {context}: {type(exc).__name__}: {exc}" if context else f"{type(exc).__name__}: {exc}"
    
    if logger.isEnabledFor(logging.DEBUG):
        # In debug mode, log with full stack trace
        logger.debug(msg, exc_info=True)
    else:
        # In normal mode, just log the error message
        logger.error(msg)


def log_api_request(logger: logging.Logger, method: str, url: str, **kwargs):
    """
    Log an API request (only in debug mode).
    
    Args:
        logger: Logger instance to use
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        **kwargs: Additional request details (headers, params, data, etc.)
    """
    if logger.isEnabledFor(logging.DEBUG):
        # Sanitize sensitive data
        sanitized_kwargs = {}
        for key, value in kwargs.items():
            if key in ('headers', 'auth'):
                sanitized_kwargs[key] = '<redacted>'
            elif key == 'data' or key == 'json':
                # Show structure but not full content
                if isinstance(value, dict):
                    sanitized_kwargs[key] = f"<dict with {len(value)} keys>"
                elif isinstance(value, (list, tuple)):
                    sanitized_kwargs[key] = f"<{type(value).__name__} with {len(value)} items>"
                else:
                    sanitized_kwargs[key] = f"<{type(value).__name__}>"
            else:
                sanitized_kwargs[key] = value
        
        extras = ', '.join(f"{k}={v}" for k, v in sanitized_kwargs.items())
        logger.debug(f"→ API {method} {url_ref(url)} {extras}")


def log_api_response(logger: logging.Logger, method: str, url: str, status_code: int, elapsed_time: Optional[float] = None):
    """
    Log an API response (only in debug mode).
    
    Args:
        logger: Logger instance to use
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        status_code: HTTP status code
        elapsed_time: Request duration in seconds (optional)
    """
    if logger.isEnabledFor(logging.DEBUG):
        msg = f"← API {method} {url_ref(url)} → {status_code}"
        if elapsed_time is not None:
            msg += f" ({elapsed_time:.3f}s)"
        logger.debug(msg)


def log_state_change(logger: logging.Logger, entity: str, old_state, new_state, reason: str = ""):
    """
    Log a state change (only in debug mode).
    
    Args:
        logger: Logger instance to use
        entity: What is changing state (e.g., "channel_123", "stream_checker")
        old_state: Previous state
        new_state: New state
        reason: Why the state changed (optional)
    """
    if logger.isEnabledFor(logging.DEBUG):
        msg = f"State change: {entity} {old_state} → {new_state}"
        if reason:
            msg += f" ({reason})"
        logger.debug(msg)
