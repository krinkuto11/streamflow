#!/bin/bash

# StreamFlow Entrypoint
# Starts Flask API directly

set -e

echo "[INFO] Starting StreamFlow Container: $(date)"

# Environment variables with defaults
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-5000}"
DEBUG_MODE="${DEBUG_MODE:-false}"
CONFIG_DIR="${CONFIG_DIR:-/app/data}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"
STREAMFLOW_RUN_AS_ROOT="${STREAMFLOW_RUN_AS_ROOT:-false}"

# Export environment variables for the Flask application
export API_HOST API_PORT DEBUG_MODE CONFIG_DIR

run_as_root=false
case "${STREAMFLOW_RUN_AS_ROOT,,}" in
    true|1|yes|on) run_as_root=true ;;
esac

if [ "$(id -u)" = "0" ] && [ "$run_as_root" != "true" ]; then
    case "$PUID:$PGID" in
        *[!0-9:]*|:*|*:)
            echo "[ERROR] PUID and PGID must be numeric." >&2
            exit 1
            ;;
    esac

    target_group="$(getent group "$PGID" | cut -d: -f1 || true)"
    if [ -z "$target_group" ]; then
        target_group="streamflow-runtime"
        groupadd --gid "$PGID" "$target_group"
    fi

    existing_user="$(getent passwd "$PUID" | cut -d: -f1 || true)"
    if [ -z "$existing_user" ] || [ "$existing_user" = "streamflow" ]; then
        usermod --uid "$PUID" --gid "$PGID" streamflow
    fi
    mkdir -p csv logs "$CONFIG_DIR"
    chown -R "$PUID:$PGID" csv logs "$CONFIG_DIR"
    echo "[INFO] Dropping runtime privileges to ${PUID}:${PGID}."
    exec gosu "$PUID:$PGID" "$0" "$@"
fi

# Deprecated: Old manual interval approach (kept for backward compatibility warnings)
if [ -n "$INTERVAL_SECONDS" ]; then
    echo "[WARNING] INTERVAL_SECONDS environment variable is deprecated."
    echo "[WARNING] The system now uses automated scheduling via the web API."
    echo "[WARNING] Please configure automation via the web interface or API endpoints."
fi

# Check if configuration files exist, create defaults if needed
echo "[INFO] Checking configuration files..."

# Ensure required directories exist (including the persisted data directory)
mkdir -p csv logs "$CONFIG_DIR"
echo "[INFO] Config directory: $CONFIG_DIR"

# Validate environment setup
echo "[INFO] Dispatcharr credentials will be configured via the Setup Wizard or loaded from the database."

# Start StreamFlow service
echo "[INFO] ============================================"
echo "[INFO] Starting StreamFlow Container"
echo "[INFO] ============================================"
echo "[INFO] Flask API: ${API_HOST}:${API_PORT}"
echo "[INFO] Debug mode: ${DEBUG_MODE}"
echo "[INFO] ============================================"
echo "[INFO] Access the web interface at http://localhost:${API_PORT}"
echo "[INFO] API documentation available at http://localhost:${API_PORT}/api/health"
echo "[INFO] ============================================"

# Start Flask API directly
echo "[INFO] Running configuration migrations..."
python3 scripts/migrate_to_sql.py

export PYTHONPATH=.

# Use exec to ensure Flask becomes PID 1 and receives signals properly
exec python3 apps/api/web_api.py --host "${API_HOST}" --port "${API_PORT}"
