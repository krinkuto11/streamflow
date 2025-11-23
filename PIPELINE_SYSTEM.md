# Pipeline System Documentation

## Overview

The StreamFlow application now supports 5 different pipeline modes, each with different behaviors for updating M3U playlists, matching streams to channels, and checking stream quality. This provides flexibility for users with different needs and connection constraints.

## Pipeline Modes

### Pipeline 1: Update → Match → Check (with 2-hour immunity)

**Configuration:**
```json
{
  "pipeline_mode": "pipeline_1",
  "queue": {
    "check_on_update": true
  }
}
```

**Behavior:**
1. Every X minutes (configurable): Update M3U playlists
2. Match new streams to channels via regex patterns
3. Check channels that received new streams (respects 2-hour immunity)
   - Only checks streams that haven't been checked in the last 2 hours
   - Uses cached scores for recently checked streams

**Use Case:** Users with moderate connection limits who want automatic updates, matching, and quality checking with immunity to prevent excessive checking.

---

### Pipeline 1.5: Pipeline 1 + Scheduled Global Action

**Configuration:**
```json
{
  "pipeline_mode": "pipeline_1_5",
  "queue": {
    "check_on_update": true
  },
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",  // or "monthly"
    "hour": 3,
    "minute": 0,
    "day_of_month": 1  // for monthly
  }
}
```

**Behavior:**
- All features of Pipeline 1
- **PLUS:** Scheduled Global Action (daily or monthly)
  - Updates all M3U playlists
  - Matches all streams
  - Checks ALL channels, bypassing 2-hour immunity
  - Typically scheduled during off-peak hours (e.g., 3 AM)

**Use Case:** Users who want automatic updates with immunity during the day, but want a complete check of all channels during off-peak hours.

---

### Pipeline 2: Update → Match only (no automatic checking)

**Configuration:**
```json
{
  "pipeline_mode": "pipeline_2",
  "queue": {
    "check_on_update": false
  }
}
```

**Behavior:**
1. Every X minutes: Update M3U playlists
2. Match new streams to channels via regex patterns
3. **NO automatic stream checking**

**Use Case:** Users with strict connection limits who only want to keep their channels populated with streams, but don't want automatic quality checking.

---

### Pipeline 2.5: Pipeline 2 + Scheduled Global Action

**Configuration:**
```json
{
  "pipeline_mode": "pipeline_2_5",
  "queue": {
    "check_on_update": false
  },
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour": 3,
    "minute": 0
  }
}
```

**Behavior:**
- All features of Pipeline 2
- **PLUS:** Scheduled Global Action (daily or monthly)
  - Updates all M3U playlists
  - Matches all streams
  - Checks ALL channels, bypassing immunity

**Use Case:** Users with connection limits who want to avoid checking during the day, but want a complete check during off-peak hours.

---

### Pipeline 3: Only Scheduled Global Action

**Configuration:**
```json
{
  "pipeline_mode": "pipeline_3",
  "queue": {
    "check_on_update": false
  },
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour": 3,
    "minute": 0
  }
}
```

**Behavior:**
- **NO automatic updates or matching**
- **ONLY:** Scheduled Global Action (daily or monthly)
  - Updates all M3U playlists
  - Matches all streams
  - Checks ALL channels

**Use Case:** Users who want complete control and only want the system to run once per day/month at a specific time.

---

## Global Action

### What is a Global Action?

A Global Action is a comprehensive operation that:
1. **Updates** all enabled M3U playlists
2. **Matches** all streams to channels via regex patterns
3. **Checks** ALL channels, bypassing the 2-hour immunity period

### When Does it Run?

Global Actions run:
- **Automatically:** Based on the scheduled time (for Pipeline 1.5, 2.5, and 3)
- **Manually:** Via the "Global Action" button in the UI or API call

### Exclusive Execution

**Important:** During a global action, all regular automation is paused to prevent concurrent operations:
- Regular M3U update cycles are skipped
- Automated stream matching is paused
- Regular channel queueing for checking is suspended
- Once the global action completes, regular automation automatically resumes

This ensures that global actions run cleanly without interference from regular operations, and prevents resource contention.

### Force Check Behavior

During a Global Action, all channels are marked for "force check" which:
- Bypasses the 2-hour immunity period
- Analyzes ALL streams in every channel (not just new ones)
- Updates all stream quality scores
- Re-ranks all channels based on fresh analysis

---

## API Endpoints

### Trigger Manual Global Action

```
POST /api/stream-checker/global-action
```

**Response:**
```json
{
  "message": "Global action triggered successfully",
  "status": "in_progress",
  "description": "Update, Match, and Check all channels in progress"
}
```

### Get Stream Checker Status

```
GET /api/stream-checker/status
```

**Response includes:**
```json
{
  "running": true,
  "global_action_in_progress": false,
  "config": {
    "pipeline_mode": "pipeline_1_5",
    "global_check_schedule": {
      "enabled": true,
      "frequency": "daily",
      "hour": 3,
      "minute": 0
    }
  },
  "last_global_check": "2025-10-13T03:00:15.123Z"
}
```

---

## Technical Implementation

### Key Classes and Methods

#### StreamCheckerService

**New Methods:**
- `_perform_global_action()`: Executes complete Update→Match→Check cycle
- `trigger_global_action()`: Manually triggers a global action
- `_queue_updated_channels()`: Now respects pipeline mode

**Updated Methods:**
- `_check_global_schedule()`: Checks pipeline mode before running
- `_queue_all_channels(force_check=False)`: Supports force checking

#### ChannelUpdateTracker

**New Methods:**
- `mark_channel_for_force_check(channel_id)`: Sets force check flag
- `should_force_check(channel_id)`: Checks if channel should be force checked
- `clear_force_check(channel_id)`: Clears force check flag

### Queue Management

The queue system prevents:
- Duplicate channel checking
- Race conditions during M3U updates
- Checking channels that are already queued or in progress
- Global actions from stacking up

### Stream Immunity System

Streams are tracked per channel to avoid redundant checking:
- Each stream's last check timestamp is stored
- When checking a channel, only unchecked streams (or those outside immunity period) are analyzed
- Recently checked streams use cached quality scores from their last analysis
- Force check bypasses this immunity and rechecks all streams
- **Configurable immunity period** via `queue.immunity_time_hours` (default: 2 hours)

This system significantly reduces API calls and analysis time for large playlists, especially beneficial for users with connection limits.

---

## Configuration Examples

### For Users Without Connection Limits
```json
{
  "pipeline_mode": "pipeline_1",
  "queue": {
    "check_on_update": true,
    "max_channels_per_run": 50,
    "immunity_time_hours": 2
  }
}
```

### For Users With Moderate Limits
```json
{
  "pipeline_mode": "pipeline_1_5",
  "queue": {
    "check_on_update": true,
    "max_channels_per_run": 20,
    "immunity_time_hours": 4
  },
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour": 3,
    "minute": 0
  }
}
```

### For Users With Strict Limits
```json
{
  "pipeline_mode": "pipeline_3",
  "queue": {
    "immunity_time_hours": 6
  },
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour": 3,
    "minute": 0
  }
}
```

---

## Migration Guide
  }
}
```

### For Users With Strict Limits
```json
{
  "pipeline_mode": "pipeline_3",
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour": 3,
    "minute": 0
  }
}
```

---

## Migration Guide

Existing installations will automatically use Pipeline 1.5 (the default). To change:

1. **Via Web UI** (Recommended):
   - Navigate to **Configuration** page
   - Select your desired pipeline from the pipeline selection cards
   - Configure schedule if using pipelines 1.5, 2.5, or 3
   - Click **Save Settings**

2. Via API:
```bash
curl -X PUT http://localhost:3000/api/stream-checker/config \
  -H "Content-Type: application/json" \
  -d '{"pipeline_mode": "pipeline_2_5"}'
```

3. Via Configuration File:
Edit `/app/data/stream_checker_config.json`:
```json
{
  "pipeline_mode": "pipeline_2_5"
}
```

Note: Changes via web UI or API take effect immediately without restart.

---

## Testing

All pipeline modes are thoroughly tested:
- 146 total tests pass
- 11 tests specifically for pipeline modes
- Tests cover:
  - Pipeline mode selection
  - Force check behavior
  - Global action functionality
  - Queue management
  - Scheduled checks

---

## UI Configuration

### Setup Wizard

The **Setup Wizard** now includes pipeline selection during initial configuration:
- Step 1: Dispatcharr Connection
- Step 2: Channel Patterns Configuration
- Step 3: **Pipeline Selection & Automation Settings**
  - Choose your pipeline mode (1, 1.5, 2, 2.5, or 3)
  - Configure schedule for pipelines with scheduled actions
  - Set update intervals and feature toggles
- Step 4: Setup Complete

This ensures new installations start with an appropriate pipeline mode for their needs.

### Configuration Page

The web interface provides a unified **Configuration** page where you can:

1. **Select Pipeline Mode**: Choose from 5 pipeline modes with clear descriptions
   - Pipeline 1: Update → Match → Check (with 2hr immunity)
   - Pipeline 1.5: Pipeline 1 + Scheduled Global Action
   - Pipeline 2: Update → Match only (no automatic checking)
   - Pipeline 2.5: Pipeline 2 + Scheduled Global Action
   - Pipeline 3: Only Scheduled Global Action

2. **Configure Schedule**: For pipelines with scheduled actions (1.5, 2.5, 3)
   - Set frequency (daily or monthly)
   - Choose exact time (hour and minute)
   - For monthly: select day of month

3. **Adjust Settings**: Only relevant settings are shown based on selected pipeline
   - Update intervals (for pipelines 1, 1.5, 2, 2.5)
   - Stream analysis parameters
   - Queue settings

### Stream Checker Page

Monitor real-time statistics and progress:
- View current pipeline and schedule
- See if a global action is currently in progress
- Manually trigger Global Action
- Monitor queue status and check progress

## Dead Stream Detection and Management

StreamFlow automatically detects and manages non-functional streams to maintain channel quality.

### How Dead Stream Detection Works

**Detection Criteria:**
A stream is considered "dead" if during quality analysis:
- Resolution is `0x0` or contains a 0 dimension (e.g., `1920x0` or `0x1080`)
- Bitrate is 0 kbps or null

**Tagging:**
When a dead stream is detected, it is automatically tagged with a `[DEAD]` prefix in Dispatcharr:
- Original: `"CNN HD"`
- Tagged: `"[DEAD] CNN HD"`

### Pipeline-Specific Behavior

#### Pipelines 1 and 1.5 (Regular Checks)
- Dead streams detected during regular channel checks
- Immediately tagged with `[DEAD]` prefix
- **Removed from channels** to maintain quality
- Will not be re-added during subsequent stream matching

#### Pipelines 2 and 2.5 (No Regular Checking)
- Pipeline 2: No stream checking, so no dead stream detection
- Pipeline 2.5: Dead streams only detected during scheduled global actions

#### Pipeline 3 (Scheduled Only)
- Dead streams only detected during scheduled global actions

### Revival During Global Actions

During global actions (force check), dead streams are given a chance to revive:
1. All streams (including dead ones) are re-analyzed
2. If a dead stream is found to be working:
   - The `[DEAD]` prefix is removed
   - Stream is restored to normal status
   - Stream can be matched to channels again
3. If still dead, the tag remains

**Example Revival:**
```
Before global check: "[DEAD] CNN HD" (resolution: 0x0, bitrate: 0)
After global check:  "CNN HD" (resolution: 1920x1080, bitrate: 5000)
```

### Stream Matching Exclusion

Dead streams are automatically excluded from stream discovery:
- When regex patterns are matched, streams with `[DEAD]` prefix are skipped
- Prevents dead streams from being added to new channels
- Ensures only functional streams are assigned

### Benefits

1. **Automatic Cleanup**: Channels stay clean without manual intervention
2. **Quality Maintenance**: Only working streams remain in channels
3. **Efficient Checking**: Dead streams don't waste resources during regular checks
4. **Revival Opportunity**: Streams can recover during global actions
5. **Clear Identification**: `[DEAD]` tag makes status immediately visible

### Configuration

No special configuration required. Dead stream detection is:
- **Enabled by default** in all pipelines with stream checking
- **Automatic** - no manual intervention needed
- **Transparent** - all actions logged in changelog

### Monitoring

Dead stream activity is logged in the changelog:
- Detection and tagging events
- Removal from channels
- Revival events during global actions

You can monitor dead streams via:
- Changelog page in web UI
- Dispatcharr stream list (search for `[DEAD]`)
- Stream checker logs

---

## Future Enhancements

Potential future improvements:
- Per-channel pipeline overrides
- Custom pipeline schedules per channel
- Analytics dashboard showing check frequency and patterns
- Dynamic pipeline adjustment based on connection speed
- Dead stream statistics and reporting dashboard
