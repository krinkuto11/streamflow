# Build for StreamFlow application
# Includes Flask API in a single container
# Frontend should be pre-built and copied to build context

FROM lscr.io/linuxserver/ffmpeg:8.1.2-cli-ls76@sha256:2e7000921be8de2704a4f27dfd3d988562697a346eaabb937a81046c306f0af7
ARG DEBIAN_FRONTEND=noninteractive

# Dispatcharr v0.31.0 uses this FFmpeg base release (67b241d). Keep the binary
# and linked libraries together; copying only the binary would not be equivalent.
# Python 3.11 remains isolated from the base image's Python.
RUN apt-get update && apt-get install --no-install-recommends -y \
    ca-certificates curl software-properties-common gosu \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install --no-install-recommends -y \
    python3.11 python3.11-venv \
    && python3.11 -m venv /opt/streamflow-venv \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/streamflow-venv/bin:${PATH}"
# The LinuxServer base exposes every NVIDIA device by default. Preserve the
# previous StreamFlow CPU default; an explicit Docker template value such as
# NVIDIA_VISIBLE_DEVICES=all still enables the configured GPU runtime.
ENV NVIDIA_VISIBLE_DEVICES=""

# Create working directory for backend
WORKDIR /app

# Copy the compiled production lock first for deterministic caching.
COPY backend/requirements.lock .

# Install only artifacts whose versions and hashes were reviewed at lock time.
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Fail the build if Python or FFmpeg is missing. The amd64 hashes were measured
# from the existing Dispatcharr 0.31.0 container on Unraid.
RUN python3 -c "import sys; assert sys.version_info[:2] == (3, 11)" \
    && ffmpeg -hide_banner -version \
    && ffprobe -hide_banner -version \
    && if [ "$(dpkg --print-architecture)" = amd64 ]; then \
        printf '%s  %s\n' \
            66d929368322861e8dcdbeb3f69e4ce8ddf89b6cc2d3c6e364424c00561bf86c /usr/local/bin/ffmpeg \
            210edb1ee14eb925069b23bca3ea6ccdb5361e4a6c281c6f4fd0346d7de65bdd /usr/local/bin/ffprobe \
            | sha256sum -c -; \
    fi

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
    CMD curl -fsS "http://127.0.0.1:${API_PORT:-5000}/api/health" >/dev/null || exit 1

# Use entrypoint script to start Flask API
ENTRYPOINT ["/app/entrypoint.sh"]

