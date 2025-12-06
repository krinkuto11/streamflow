# Features

## Stream Management

### Pipeline-Based Automation
StreamFlow offers 5 pipeline modes to match different usage scenarios:
- **Pipeline 1**: Continuous updates with 2-hour immunity (moderate connection usage)
- **Pipeline 1.5**: Pipeline 1 + scheduled complete checks (balanced approach)
- **Pipeline 2**: Updates and matching only, no automatic checking (minimal connection usage)
- **Pipeline 2.5**: Pipeline 2 + scheduled complete checks (controlled automation)
- **Pipeline 3**: Only scheduled operations (maximum control)

See [PIPELINE_SYSTEM.md](PIPELINE_SYSTEM.md) for detailed pipeline documentation.

### Automated M3U Playlist Management
- Automatically refreshes playlists every 5 minutes (configurable)
- Detects playlist changes in real-time
- Updates channels immediately when M3U refreshes
- Tracks update history in changelog

### Intelligent Stream Quality Checking
Multi-factor analysis of stream quality with two configurable modes:

#### Full Mode (Default)
- **Bitrate**: Average kbps measurement (optimized for MPEG-TS streams)
- **Resolution**: Width × height detection
- **Frame Rate**: FPS analysis
- **Video Codec**: H.265/H.264 identification
- **Audio Codec**: Detection and validation
- **Error Detection**: Decode errors, discontinuities, timeouts

**Technical Implementation:**
- Uses ffprobe with configurable probe duration (default: 10 seconds)
- Network timeout protection via `-rw_timeout` to prevent hanging on buffering streams
- `-analyzeduration` and `-probesize` set based on probe duration to ensure proper bitrate detection
- Best for accurate stream ranking but takes longer per stream
- Streams without bitrate are marked as dead
- Default timeout: 45 seconds (allows adequate time for buffering)

#### Quick Mode
- **Resolution**: Width × height detection
- **Frame Rate**: FPS analysis
- **Video Codec**: H.265/H.264 identification
- **Audio Codec**: Detection and validation
- **Error Detection**: Decode errors, discontinuities, timeouts

**Technical Implementation:**
- Uses ffprobe with minimal probe duration (1 second, increased for buffering)
- Network timeout protection via `-rw_timeout` to prevent hanging
- Skips bitrate analysis for faster checking
- Best for large playlists or when speed is priority
- Scoring relies on resolution, FPS, and codec only
- Streams without bitrate are NOT marked as dead
- Faster but still allows for basic stream buffering

**Mode Selection:** Configure in the Stream Analysis Settings under Configuration page. Choose based on your needs:
- Use **Full Mode** for accurate bitrate-based stream ranking
- Use **Quick Mode** for faster checks when bitrate is not critical

### Automatic Stream Reordering
- Best quality streams automatically moved to top
- Quality score calculation (0.0-1.0 scale)
- Configurable scoring weights
- Preserves stream availability

### Stream Discovery
- Regex pattern matching for automatic assignment
- New stream detection on playlist refresh
- Automatic channel assignment based on patterns
- Pattern testing interface

## Quality Analysis

### Scoring Formula
**Total Score = (Bitrate × 0.30) + (Resolution × 0.25) + (FPS × 0.15) + (Codec × 0.10) + (Errors × 0.20)**

### Configurable Weights
```json
{
  "weights": {
    "bitrate": 0.30,      // Default: 30%
    "resolution": 0.25,   // Default: 25%
    "fps": 0.15,          // Default: 15%
    "codec": 0.10,        // Default: 10%
    "errors": 0.20        // Default: 20%
  }
}
```

### Codec Preferences
- H.265/HEVC preference: Higher score for modern codecs

### Sequential Checking
- One channel at a time to avoid overload
- Protects streaming providers from concurrent requests
- Queue-based processing
- Real-time progress tracking

### Dead Stream Detection and Management
Automatically identifies and manages non-functional streams:
- **Detection**: Streams with resolution=0 or bitrate=0 are marked as dead
- **Tagging**: Dead streams are prefixed with `[DEAD]` in Dispatcharr
- **Removal**: Dead streams are removed from channels during regular checks
- **Revival Check**: During global actions, dead streams are re-checked for revival
- **Matching Exclusion**: Dead streams are not assigned to channels during stream matching
- **Pipeline-Aware**: Only operates in pipelines with stream checking enabled (1, 1.5, 2.5, 3)

## User Interface

### Dashboard
- System status overview
- Recent activity display
- Quick action buttons (start/stop automation)
- Real-time statistics

### Stream Checker Dashboard
- Service status monitoring
- Real-time statistics (queue size, completed, failed)
- Progress tracking with detailed stream information
- Pipeline information display
- Global Action trigger button
- Queue management (clear queue)

### Channel Configuration
- Visual regex pattern editor
- Pattern testing interface
- Live stream matching preview
- Enable/disable patterns

### Configuration Page (unified)
- **Pipeline Selection**: Choose from 5 automation modes with visual cards
- **Schedule Configuration**: Set timing for global actions (pipelines 1.5, 2.5, 3)
  - Daily or monthly frequency
  - Precise time selection (hour and minute)
  - Day of month for monthly schedules
- **Context-Aware Settings**: Only relevant options shown based on selected pipeline
- **Update Intervals**: Configure M3U refresh frequency (for applicable pipelines)
- **Stream Analysis Parameters**: User agent, timeouts, retries
- **Queue Settings**: Maximum queue size, channels per run

### Changelog
- Complete activity history
- Date range filtering
- Action categorization
- Detail expansion

### Setup Wizard
- Guided initial configuration
- Dispatcharr connection testing
- Configuration validation
- Quick start assistance

## Automation Features

### M3U Update Tracking
- Automatic detection of playlist updates
- Immediate channel queuing on update
- Update timestamp tracking
- Prevents duplicate checking

### Global Actions
- **Manual Trigger**: One-click complete update cycle (Update → Match → Check all)
- **Scheduled Execution**: Automatic runs at configured time (daily or monthly)
- **Force Check**: Bypasses 2-hour immunity to check all streams
- **Pipeline Integration**: Available in pipelines 1.5, 2.5, and 3
- **Configurable Timing**: Precise hour and minute selection for off-peak operation

### Queue Management
- Priority-based queue system
- Manual channel addition
- Queue clearing
- Duplicate prevention

### Real-Time Progress
- Current channel display
- Stream-by-stream progress
- Quality score updates
- Error reporting

## Data Management

### Changelog
- All automation actions logged
- Timestamps and details
- Persistent storage
- Filterable history

### Configuration Persistence
- All settings in JSON files
- Docker volume mounting
- Easy backup and restore
- Version-agnostic format

### Setup Wizard
- First-run configuration
- Connection validation
- Default settings
- Quick deployment

## API Integration

### REST API
- 30+ endpoints
- JSON request/response
- Authentication support
- Error handling

### Real-Time Updates
- Polling for status
- Progress tracking
- Queue monitoring
- Statistics updates

### Dispatcharr Integration
- Full API support
- Token-based authentication
- Channel management
- Stream operations

## Technical Features

### Docker Deployment
- Single container architecture
- Volume-based persistence
- Environment variable configuration
- Health checks

### Error Handling
- Automatic retry logic
- Error logging
- Graceful degradation
- User notifications

### Performance
- Sequential stream checking
- Efficient queue processing
- Minimal API calls
- Resource optimization

### Logging
- Comprehensive activity logs
- Error tracking
- Debug mode support
- Persistent changelog
