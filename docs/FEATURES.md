# Features

## Configuration Management

### Dispatcharr Connection Settings
- **Centralized Configuration**: Configure Dispatcharr connection in the Automation Settings page
- **Connection Testing**: Test connection to Dispatcharr with instant feedback
- **Secure Credentials**: Password storage with masking
- **Wizard Integration**: Configuration available in both Setup Wizard and Settings

### M3U Account Auto-Discovery
- **Automatic Detection**: New M3U accounts added in Dispatcharr are automatically discovered during playlist updates
- **Seamless Integration**: No manual configuration needed for new accounts
- **Real-time Updates**: Account list refreshed after every playlist refresh
- **UDI Integration**: Uses Universal Data Index (UDI) for efficient caching

### Channel Profile Management
- **Profile Selection**: Choose specific Dispatcharr channel profiles instead of the general channel list
- **Dead Stream Management**: Automatically disable channels with no working streams in a target profile
- **Snapshot-Based Re-enabling**: Create snapshots to track which channels should be re-enabled when streams return
- **Selective Control**: Different profiles can serve different purposes (e.g., main profile, disabled channels profile)
- **Manual Triggers**: Manually trigger empty channel disabling at any time
- **Visual Management**: User-friendly interface in Automation Settings with snapshot information display

See [CHANNEL_PROFILES_FEATURE.md](CHANNEL_PROFILES_FEATURE.md) for detailed profile management documentation.

## Stream Management

### Channel Ordering Interface
- **Drag-and-Drop Reordering**: Intuitive drag-and-drop interface for channel organization
- **Multiple Sorting Options**:
  - Sort by channel number (ascending)
  - Sort by name (alphabetical)
  - Sort by ID
  - Custom manual order
- **Visual Feedback**: Real-time updates with unsaved changes indicator
- **Bulk Operations**: Save all changes at once
- **Reset Option**: Discard changes and revert to last saved order

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
- **M3U Priority System**: Configure stream selection priority
  - **Global Priority Mode**: Single mode applies to all M3U accounts
    - Disabled: Priority ignored, streams selected by quality only
    - Same Resolution Only: Priority applied within same resolution groups
    - All Streams: Always prefer higher priority accounts regardless of quality
  - Per-account priority values (0-100)
  - Priority fields disabled when mode is "disabled"
  - Only enabled/active playlists shown in priority UI

### Intelligent Stream Quality Checking
Multi-factor analysis of stream quality using a single optimized ffmpeg call:
- **Bitrate**: Average kbps measurement
- **Resolution**: Width × height detection
- **Frame Rate**: FPS analysis
- **Video Codec**: H.265/H.264 identification with automatic sanitization
  - Filters out invalid codec names (e.g., "wrapped_avframe", "unknown")
  - Extracts actual codec from hardware-accelerated streams
- **Audio Codec**: Detection and validation
  - Parses **input stream codecs** only (e.g., "aac", "ac3", "mp3", "eac3")
  - Avoids decoded output formats (e.g., "pcm_s16le")
  - Supports multiple audio streams and language tracks
- **Error Detection**: Decode errors, discontinuities, timeouts
- **Optimized Performance**: Single ffmpeg call instead of separate ffprobe+ffmpeg (reduced overhead)
- **Parallel Checking**: Thread-based concurrent analysis with configurable worker pool
  - Proper pipeline: gather stats in parallel → when ALL checks finish → push info to Dispatcharr
  - Prevents race conditions during concurrent operations

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

### Match Profiles (Advanced Stream Matching)
- **Visual Pipeline Builder**: Drag-and-drop interface for creating matching workflows
- **Granular Control**: Fine-tune stream-to-channel matching with multiple stages
- **Reusable Profiles**: Save and manage multiple match configurations
- **Pipeline Nodes**:
  - **Source**: Filter streams by M3U accounts and groups
  - **Filter**: Apply regex patterns and quality filters
  - **Transform**: Transform stream names (remove prefixes, normalize whitespace)
  - **Match**: Match streams to channels with configurable patterns
  - **Action**: Define actions (add to channel, tag, skip)
- **Priority System**: Control execution order with profile priorities
- **Testing Mode**: Preview matches before applying
- **Backward Compatible**: Works alongside legacy regex patterns
- **Mass Assignment**: Efficiently assign matching rules to multiple channels

See [MATCH_PROFILES_IMPLEMENTATION.md](MATCH_PROFILES_IMPLEMENTATION.md) for detailed match profiles documentation.

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
- Interlaced penalty: Lower score for interlaced content
- Dropped frames penalty: Lower score for streams with frame drops

### Sequential and Parallel Checking
- **Parallel Mode** (default): Concurrent stream checking with configurable worker pool (default: 10)
  - Thread-based parallel execution
  - Configurable global concurrency limit
  - **Per-Account Stream Limits**: Respects maximum concurrent streams for each M3U account
    - Smart scheduler ensures account limits are never exceeded
    - Multiple accounts can check streams in parallel
    - Example: Account A (limit: 1), Account B (limit: 2) with streams A1, A2, B1, B2, B3
      - Concurrently checks: A1, B1, B2 (3 total, respecting limits)
      - When A1 completes, A2 starts; when B1/B2 completes, B3 starts
  - Stagger delay to prevent simultaneous starts
  - Robust pipeline: all stats gathered in parallel, then pushed to Dispatcharr after ALL checks complete
  - Prevents race conditions with dead stream removal
- **Sequential Mode**: One stream at a time for minimal provider load
- Queue-based processing
- Real-time progress tracking

### Dead Stream Detection and Management
Automatically identifies and manages non-functional streams:
- **Detection**: Streams with resolution=0 or bitrate=0 are marked as dead
- **Changelog Tracking**: Dead streams show status "dead" in changelog (not score:0)
- **Revival Tracking**: Revived streams show status "revived" in changelog
- **Removal**: Dead streams are removed from channels during regular checks
- **Matching Exclusion**: Dead streams are not assigned to channels during stream matching
- **Pipeline-Aware**: Only operates in pipelines with stream checking enabled (1, 1.5, 2.5, 3)

#### Dead Stream Revival: Global Action vs Single Channel Check
There is an important difference in how dead streams are handled:

**Global Action** (gives ALL dead streams a second chance):
1. **Step 1**: Refresh UDI cache
2. **Step 2**: Clear ALL dead streams from tracker (giving them a second chance)
3. **Step 3**: Update all M3U playlists
4. **Step 4**: Match and assign streams (including previously dead ones since tracker was cleared)
5. **Step 5**: Check all channels (force check bypasses immunity)
6. **Result**: ALL previously dead streams can be re-added and re-checked

**Single Channel Check** (gives dead streams for THAT CHANNEL a second chance):
1. **Step 1**: Identify M3U accounts used by the channel
2. **Step 2**: Refresh playlists for those M3U accounts
3. **Step 3**: Clear dead streams for THAT CHANNEL from tracker (giving them a second chance)
4. **Step 4**: Re-match and assign streams (if matching_mode is enabled for the channel)
5. **Step 5**: Force check all streams (if checking_mode is enabled for the channel, bypasses 2-hour immunity)
6. **Result**: Dead streams for that channel are cleared and can be re-added and re-checked

**Channel Settings Override**:
Single Channel Check respects per-channel `matching_mode` and `checking_mode` settings:
- If `matching_mode='disabled'`: Stream matching (Step 4) is skipped
- If `checking_mode='disabled'`: Stream quality checking (Step 5) is skipped
- Steps 1-3 (playlist refresh, dead stream clearing) always execute regardless of settings
- This allows users to configure channels individually (e.g., match but don't check, or check but don't match)

**Why the difference?**
- Global Action is the comprehensive system-wide operation that clears the slate and gives every stream a fresh chance
- Single Channel Check is a targeted operation that only affects the specified channel
- This ensures efficient targeted operations while respecting per-channel configuration

**When dead streams get a second chance:**
- During scheduled Global Actions (pipelines 1.5, 2.5, and 3)
- During manually triggered Global Actions
- During Single Channel Check for that specific channel
- When streams are manually re-added to channels outside of the automated system

## User Interface

### Theme Customization
- **Dark/Light Mode Toggle**: Switch between light, dark, and auto (system) themes
- **Deep Black Dark Mode**: True black background (#000) with white text and dark green accents
- **System Preference Detection**: Automatically follows system theme in auto mode
- **Persistent Settings**: Theme preference saved to local storage

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

### EPG-Based Scheduling
The Scheduling page provides powerful tools for managing channel checks before important programs:

#### Manual Scheduled Events
- **Schedule channel checks before specific EPG programs**
  - Browse programs from Dispatcharr EPG data
  - Select channel and program
  - Set check timing (minutes before program starts)
- **Playlist refresh included**: Each scheduled check also refreshes playlists
- **Event management**: View all scheduled events with delete capability

#### Auto-Create Rules (Regex-Based)
Automatically create scheduled events based on program name patterns:

- **Rule Configuration**:
  - Rule name for easy identification
  - Channel selection with search (supports multiple channels)
  - Regex pattern to match program names
  - Minutes before setting (when to run check)

- **Live Regex Testing**:
  - Test patterns against real EPG data
  - See matching programs before creating rule
  - Case-insensitive matching for flexibility

- **Import/Export Rules**:
  - **Export**: Download all rules as JSON file for backup or transfer
    - Click "Export" button to download rules
    - Rules are exported with essential fields only (name, channels, pattern, timing)
    - Compatible with import format
  - **Import**: Load rules from JSON file
    - **Main Import**: Use "Import" button next to "Export" to bulk import rules
    - **Wizard Import**: Use "Import JSON" button in the rule creation dialog to load a single rule into the form
    - Validates all rules before import
    - Shows detailed import results (imported/failed counts)
    - Skips invalid rules and reports errors
  - **Use Cases**:
    - Backup rules before making changes
    - Transfer rules between environments
    - Share rule configurations with other users
    - Quick setup with pre-configured rule sets

- **Automatic Event Creation**:
  - **Automatic EPG Refresh**: Background processor fetches EPG data periodically
  - Configurable refresh interval (default: 60 minutes, minimum: 5 minutes)
  - Rules scan EPG automatically on every refresh
  - Creates events for matching programs without manual intervention
  - **Smart Filtering**:
    - Skips programs that have already started or are in the past
    - Prevents re-creation of events that have already been checked
    - Tracks executed events for 7 days to avoid duplicates
  - **Duplicate Prevention**: Same channel/date/time within 5 minutes treated as duplicate
  - **Smart Updates**: Adjusts event title and time if program changes (within duplicate window)

- **Background Processor**:
  - Auto-starts with the application
  - Runs continuously in the background
  - First refresh occurs 5 seconds after startup
  - Subsequent refreshes based on configured interval
  - Manual trigger available via API
  - Graceful error handling and retry logic

- **Rule Management**:
  - View all active rules
  - Edit existing rules
  - Delete rules when no longer needed
  - Rules table shows channel(s), pattern, and timing
  - Multi-channel support for rules that apply across channels

**Use Cases**:
- Breaking news alerts: `^Breaking News|^Special Report`
- Live sports: `^Live:|Championship|Finals`
- Show-specific: `^Game of Thrones|^The Mandalorian`
- Time-specific: `Monday Night Football` on specific channels

**Example Workflow**:
1. Create rule: "Breaking News" on CNN with pattern `^Breaking`
2. EPG refresh processor automatically fetches data every 60 minutes
3. Any program starting with "Breaking" automatically gets a scheduled check
4. If program name or time changes, event updates automatically
5. Check happens X minutes before program starts
6. No manual intervention required - fully automatic!

### Channel Configuration
- **Three Main Tabs**: Comprehensive channel management interface
  - **Regex Configuration**: Pattern-based stream matching for individual channels
  - **Group Management**: Bulk settings for entire channel groups
  - **Channel Order**: Drag-and-drop channel organization
- **Horizontal Channel Cards**: Modern card-based layout with expandable sections
  - Channel logo display (wider than taller for better visibility)
  - Channel name and metadata
  - Real-time statistics:
    - Total stream count
    - Dead stream count
    - Most common resolution
    - Average bitrate (Kbps)
  - Quick actions: Edit Regex, **Check Channel**
- **Group Management Features**: Control settings for entire channel groups at once
  - **Stream Matching Toggle**: Enable/disable stream matching for all channels in a group
  - **Stream Checking Toggle**: Enable/disable quality checking for all channels in a group
  - **Visibility Control**: Channels from groups with both settings disabled are hidden from other tabs
  - **Channel Count Display**: See how many channels are in each group
  - **Bulk Operations**: Efficiently manage large numbers of channels
  - **Persistent Settings**: Group settings stored to disk and survive restarts
- **Per-Channel Settings**: Fine-grained control over each channel's behavior
  - **Matching Mode**: Control whether the channel participates in stream matching
    - `enabled` (default): Channel included in automatic stream discovery and assignment
    - `disabled`: Channel excluded from stream matching operations
  - **Checking Mode**: Control whether the channel participates in stream quality checking
    - `enabled` (default): Channel streams are automatically checked for quality
    - `disabled`: Channel streams are excluded from quality checking operations
  - **Use Cases**:
    - Match but don't check: Keep channels updated with new streams but skip quality checks
    - Check but don't match: Only check existing streams without adding new ones
    - Disable both: Fully manual channel management
  - **Settings Respected By**:
    - Global Actions (for checking_mode)
    - Single Channel Check (for both matching_mode and checking_mode)
    - Automated stream discovery (for matching_mode)
- **Single Channel Check**: Immediately check a specific channel's streams
  - Synchronous checking with detailed feedback
  - Shows results: total streams, dead streams, avg resolution, avg bitrate
  - Updates channel stats after completion
  - Creates changelog entry for tracking
  - **Respects channel settings**: Will skip matching or checking based on channel configuration
- **Expandable Regex Editor**: Toggle pattern list within each card
  - View all configured patterns for the channel
  - Add new patterns inline
  - Delete patterns individually
- **Individual Channel Checking**: Queue single channels for immediate quality checking
- **Live Statistics**: Auto-loading channel stats from backend API
- **Search and Filtering**: Find channels by name, number, or ID
- **Pagination**: Efficient browsing of large channel lists
  - Configurable items per page (10, 20, 50, 100)
  - First/Previous/Next/Last navigation buttons
  - Visual page number selection (shows 5 pages at a time)
  - Current range indicator (e.g., "Showing 1-20 of 150 channels")
  - Automatic reset to first page when search query changes
- **Export/Import**: Backup and restore regex patterns as JSON

### Changelog Page
- **Activity History**: View all system events and stream operations
- **Structured Entries**: Multiple action types:
  - **Playlist Update & Match**: Shows streams added and channels checked
  - **Global Check**: Displays all channels checked during scheduled global actions
  - **Single Channel Check**: Individual channel check results
  - **Batch Stream Check**: Consolidated entries for checking batches
- **Global Statistics**: Summary stats for each action (total channels, successful/failed checks, streams analyzed, dead streams, etc.)
- **Collapsible Subentries**: Expandable dropdown blocks for detailed information
  - **Update & Match** group: Lists streams added to each channel
  - **Check** group: Shows per-channel statistics and scores
- **Time Filtering**: View entries from last 24 hours, 7 days, 30 days, or 90 days
- **Color-Coded Badges**: Visual indicators for different action types
- **Stream Details**: Top streams with resolution, bitrate, codec, and FPS information

### Scheduling Page
- **EPG-Based Event Scheduling**: Schedule channel checks before program events
- **Channel Selection**: Searchable dropdown with all available channels
- **Program Browser**: Scrollable list of upcoming programs for selected channel
  - Displays program title, start/end times, and descriptions
  - Programs loaded from Dispatcharr's EPG grid endpoint
- **Flexible Timing**: Input field for minutes before program start
- **Event Management Table**: View all scheduled events
  - Channel logo and name
  - Program title and time
  - Scheduled check time
  - Delete action button
- **Configuration Options**: Adjust EPG data refresh interval

### Configuration Page (unified)
- **Pipeline Selection**: Choose from 5 automation modes with visual cards and hints
  - Descriptive hints for each pipeline mode
- **Schedule Configuration**: Set timing for global actions (pipelines 1.5, 2.5, 3)
  - Daily or monthly frequency
  - Precise time selection (hour and minute)
  - Day of month for monthly schedules
- **Concurrent Stream Checking**: Configure maximum parallel workers (default: 10)
  - Controls load on streaming providers
  - Adjustable stagger delay between task dispatches
- **Context-Aware Settings**: Only relevant options shown based on selected pipeline
- **Update Intervals**: Configure M3U refresh frequency (for applicable pipelines)
- **Stream Analysis Parameters**: 
  - FFmpeg duration (seconds to analyze each stream)
  - Base timeout for operations
  - **Stream Startup Buffer**: Configurable buffer time (5-120s, default: 10s) for stream startup
    - Allows high-quality streams that take longer to start to be properly analyzed
    - Total timeout = base timeout + duration + startup buffer
  - Retry attempts and delay between retries
  - User agent string for FFmpeg/FFprobe
- **Queue Settings**: 
  - Maximum queue size
  - Channels per run
  - **Regex Validation**: Toggle to validate existing streams against regex patterns
    - Removes streams from channels that no longer match patterns
    - Useful when providers change stream names
    - Runs before stream checking in global actions

### Changelog
- Complete activity history
- Date range filtering
- Action categorization
- Detail expansion

### Setup Wizard
- Guided initial configuration
- Dispatcharr connection testing
- **JSON Pattern Import**: Import channel regex patterns from JSON file
- **Pipeline Hints**: Inline descriptions for each pipeline mode
- **Smart Navigation**: Save settings automatically when advancing
- **Autostart Default**: Automation enabled by default
- Configuration validation
- Quick start assistance
- **Bug Fixes (Dec 2025)**:
  - Fixed empty regex pattern entries appearing in step 1 table
  - Reduced logo download log noise in non-debug mode

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
- **Batch Consolidation**: Stream checks batched into consolidated entries
  - Single entry per checking batch instead of per-channel entries
  - Aggregate statistics at batch level (total channels, streams analyzed, dead streams, etc.)
  - Individual channel results shown as expandable subentries
  - Cleaner, more organized changelog view

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

## EPG-Based Scheduling

### Scheduled Channel Checks Before Events
Schedule channel checks to run before EPG program events for optimal stream quality:
- **EPG Integration**: Fetches program data from Dispatcharr's EPG grid endpoint
- **Automatic Refresh**: Background processor automatically fetches EPG data periodically
  - Configurable refresh interval (default: 60 minutes, minimum: 5 minutes)
  - Auto-starts with the application
  - First refresh occurs 5 seconds after startup
  - Continues running in the background
- **Program Search**: Browse upcoming programs by channel
- **Flexible Timing**: Configure minutes before program start to run the check
- **Playlist Updates**: Playlist refresh triggered before scheduled checks
- **Event Management**: Create, view, and delete scheduled events

### User Workflow - Manual Event Creation
1. Navigate to Scheduling section in the UI
2. Click "Add Event Check" button
3. Select a channel from searchable dropdown
4. View and select from channel's upcoming programs
5. Specify minutes before program start for the check
6. Save the scheduled event
7. Monitor scheduled events in table with channel logos and program details

### User Workflow - Auto-Create Rules
1. Navigate to Scheduling section in the UI
2. Click "Create Auto-Create Rule" button
3. Configure rule name, channel, and regex pattern
4. Test pattern against live EPG data (optional)
5. Set minutes before program start
6. Save the rule
7. Events are automatically created as EPG refreshes
8. No further action required!

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
- Parallel stream checking with configurable worker pool
- Optimized single ffmpeg call (instead of ffprobe + ffmpeg)
- Efficient queue processing
- Minimal API calls
- Resource optimization

### Logging
- Comprehensive activity logs
- Error tracking
- Debug mode support
- Persistent changelog
