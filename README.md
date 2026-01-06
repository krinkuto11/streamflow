# StreamFlow for Dispatcharr
![Docker Pulls](https://ghcr-badge.elias.eu.org/shield/krinkuto11/streamflow/streamflow)

Automated stream management system for Dispatcharr IPTV services with intelligent quality checking and automatic stream reordering.
1. Updates playlists.
2. Adds Streams to channels via Regex
3. Checks streams
4. Reorders the streams to ensure the best ones are first

## Quick Deployment

```bash
git clone https://github.com/krinkuto11/streamflow.git
cd streamflow
cp .env.template .env
# Edit .env with your Dispatcharr instance details
docker compose up -d
```

**Access**: http://localhost:5000

For **local development** with hot-reload, see [DEVELOPMENT.md](DEVELOPMENT.md).

See [Deployment Guide](docs/DEPLOYMENT.md) for detailed instructions.

## Features

- **5 Pipeline Modes**: Choose the automation level that fits your needs (from continuous checking to scheduled-only)
- **Parallel Stream Checking**: Thread-based parallel stream analysis with configurable worker pool
- **Per-Account Stream Limits**: Intelligent concurrent stream limiting respects M3U provider limits while maximizing parallelism
- **AceStream Monitoring**: Real-time monitoring of AceStream channels with health tracking and automatic stream reordering by quality
  - **HTTP Method (Default)**: Lightweight monitoring using HTTP range requests (~95% less resource usage than FFmpeg)
  - **FFmpeg Method**: Traditional approach with detailed stream metrics
- **Channel Profile Management**: Select specific Dispatcharr profiles, manage dead streams, and use snapshots for automatic re-enabling
- **Automated M3U Playlist Management**: Refresh playlists every 5 minutes (configurable)
- **Stream Quality Checking**: Analyze streams for bitrate, resolution, FPS, codec quality, and errors
- **Automatic Stream Reordering**: Best quality streams moved to the top
- **Stream Discovery**: Regex patterns for automatic stream-to-channel assignment
- **Mass Regex Assignment**: Add a single regex pattern to multiple channels at once with support for channel name variables
- **Global Action**: Manual or scheduled complete update cycles (Update → Match → Check all channels)
- **Channel-Specific Settings**: Exclude individual channels from matching and/or checking operations
- **EPG-Based Scheduling**: Schedule channel checks based on EPG program data
- **JSON-backed Storage**: Fast, file-based data access with UDI (Universal Data Index)
- **Web Interface**: React-based UI with unified configuration page and real-time monitoring
- **REST API**: Full API access for all operations

## Architecture

**Single Docker container** with:
- Flask backend (Python) serving REST API
- React frontend for web interface
- Persistent configuration storage via Docker volumes
- Single port (5000) for all web access
- Multi-platform support: linux/amd64, linux/arm64

The Flask API runs directly as PID 1 in the container for simplified deployment.

## Configuration

All configuration stored in JSON files in `/app/data` (Docker volume):
- `automation_config.json` - Automation settings (intervals, features)
- `stream_checker_config.json` - Pipeline mode, scheduling, and stream checking parameters
- `channel_regex_config.json` - Regex patterns for stream assignment
- `profile_config.json` - Channel profile configuration and snapshots
- `channel_updates.json` - Channel update tracking
- `changelog.json` - Activity history

**Web UI**: Navigate to the **Configuration** page (formerly "Automation Settings") to:
- Select your pipeline mode (determines when and how streams are checked)
- Configure scheduled global actions (for pipelines 1.5, 2.5, and 3)
- Adjust update intervals and analysis settings

**Stream Checker**: View real-time statistics, progress, and manually trigger global actions

## Project Structure

```
backend/
  ├── automated_stream_manager.py    # Core automation engine
  ├── stream_checker_service.py      # Stream quality checking
  ├── web_api.py                      # Flask REST API
  ├── api_utils.py                    # Dispatcharr API utilities
  └── ...
frontend/
  └── src/
      ├── components/                 # React components
      └── services/                   # API client
docs/
  ├── DEPLOYMENT.md                   # Detailed deployment guide
  ├── API.md                          # API documentation
  └── FEATURES.md                     # Feature details
```

## Documentation

- [Deployment Guide](docs/DEPLOYMENT.md) - Installation and deployment instructions
- [API Documentation](docs/API.md) - REST API endpoints and usage
- [Features](docs/FEATURES.md) - Detailed feature descriptions
- [AceStream Monitoring](docs/ACESTREAM_MONITORING_IMPLEMENTATION.md) - AceStream monitoring setup and usage
- [AceStream HTTP vs FFmpeg](docs/ACESTREAM_HTTP_VS_FFMPEG.md) - Comparison of monitoring methods
- [Channel Profiles](docs/CHANNEL_PROFILES_FEATURE.md) - Profile management and dead stream handling
- [Concurrent Stream Limits](docs/CONCURRENT_STREAM_LIMITS.md) - Per-account stream limiting
- [Pipeline System](docs/PIPELINE_SYSTEM.md) - Automation pipeline modes
- [Debug Mode Guide](docs/DEBUG_MODE.md) - Troubleshooting with enhanced logging

## Requirements

- Docker and Docker Compose
- Dispatcharr instance with API access
- Sufficient resources for parallel stream checking (recommended: 2 CPU cores, 2GB RAM minimum)

## License

See [LICENSE](LICENSE) file for details.
