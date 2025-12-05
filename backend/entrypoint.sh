#!/bin/bash

# Dispatcharr Stream Manager - New Automated System Entrypoint
# This replaces the old watchdog pattern with the new automated stream management system

set -e

echo "[INFO] Starting Dispatcharr Automated Stream Management System: $(date)"

# Environment variables with defaults
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-5000}"
DEBUG_MODE="${DEBUG_MODE:-false}"
CONFIG_DIR="${CONFIG_DIR:-/app/data}"

# Check if configuration files exist, create defaults if needed
echo "[INFO] Checking configuration files..."

# Ensure required directories exist (including the persisted data directory)
mkdir -p csv logs "$CONFIG_DIR"
echo "[INFO] Config directory: $CONFIG_DIR"

# Validate environment setup
if [ ! -f ".env" ]; then
    echo "[WARNING] No .env file found. Using environment variables for configuration."
    echo "[INFO] Ensure DISPATCHARR_BASE_URL, DISPATCHARR_USER, and DISPATCHARR_PASS are set."
    
    # Check if required environment variables are set
    if [ -z "$DISPATCHARR_BASE_URL" ] || [ -z "$DISPATCHARR_USER" ] || [ -z "$DISPATCHARR_PASS" ]; then
        echo "[ERROR] Required environment variables not found:"
        echo "[ERROR] DISPATCHARR_BASE_URL, DISPATCHARR_USER, and DISPATCHARR_PASS must be set"
        echo "[INFO] You can either:"
        echo "[INFO] 1. Set environment variables in docker-compose.yml, or"
        echo "[INFO] 2. Copy .env.template to .env and configure your settings."
        exit 1
    fi
else
    echo "[INFO] Using .env file for configuration."
fi

# Start the automated stream management web API
echo "[INFO] Starting Web API server on ${API_HOST}:${API_PORT}"
echo "[INFO] Debug mode: ${DEBUG_MODE}"
echo "[INFO] Access the web interface at http://localhost:${API_PORT}"
echo "[INFO] API documentation available at http://localhost:${API_PORT}/api/health"

# Add health endpoint for Docker health checks
if [ "$DEBUG_MODE" = "true" ]; then
    exec python3 web_api.py --host "$API_HOST" --port "$API_PORT" --debug
else
    exec python3 web_api.py --host "$API_HOST" --port "$API_PORT"
fi

