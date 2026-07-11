# Build for StreamFlow application
# Includes Flask API in a single container
# Frontend should be pre-built and copied to build context

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create working directory for backend
WORKDIR /app

# Copy backend requirements first for better caching
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt

# Copy backend application code
COPY backend/ ./

# Copy pre-built frontend to static directory
COPY frontend/build ./static

# Create necessary directories
# data directory will be mounted as volume for persistence
RUN mkdir -p csv logs data

# The entrypoint prepares mounted paths as root, then drops to this account.
RUN groupadd --gid 10001 streamflow \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin streamflow

# Set environment variable for config directory
ENV CONFIG_DIR=/app/data
ENV PUID=99
ENV PGID=100
ENV STREAMFLOW_RUN_AS_ROOT=false

# Normalize line endings for Windows build contexts and set permissions for entrypoint
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Create default configuration files in the data directory
RUN python3 apps/core/create_default_configs.py

# Expose the Flask port
EXPOSE 5000

# Health check for Flask API
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:5000/api/health || exit 1

# Use entrypoint script to start Flask API
ENTRYPOINT ["/app/entrypoint.sh"]

