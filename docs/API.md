# API Documentation

All API endpoints are accessible at `http://localhost:3000/api/`

## Stream Checker Endpoints

### Get Status
```
GET /api/stream-checker/status
```
Returns service status, statistics, and queue information.

**Response:**
```json
{
  "running": true,
  "current_channel": "Channel Name",
  "queue_size": 5,
  "statistics": {
    "total_checked": 150,
    "total_failed": 3,
    "total_improved": 120
  }
}
```

### Start Service
```
POST /api/stream-checker/start
```
Starts the stream checking service.

### Stop Service
```
POST /api/stream-checker/stop
```
Stops the stream checking service.

### Get Queue
```
GET /api/stream-checker/queue
```
Returns current queue of channels pending check.

### Add to Queue
```
POST /api/stream-checker/queue/add
Content-Type: application/json

{
  "channel_ids": [1, 2, 3]
}
```
Adds specific channels to the checking queue.

### Clear Queue
```
POST /api/stream-checker/queue/clear
```
Removes all pending checks from the queue.

### Get Configuration
```
GET /api/stream-checker/config
```
Returns current stream checker configuration.

### Update Configuration
```
PUT /api/stream-checker/config
Content-Type: application/json

{
  "enabled": true,
  "global_check_schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour": 3,
    "minute": 0
  },
  "queue": {
    "check_on_update": true,
    "max_channels_per_run": 50
  },
  "scoring": {
    "weights": {
      "bitrate": 0.30,
      "resolution": 0.25,
      "fps": 0.15,
      "codec": 0.10,
      "errors": 0.20
    }
  }
}
```
Updates stream checker configuration.

**Configuration Options:**
- `enabled` - Enable/disable the stream checker service
- `global_check_schedule.enabled` - Enable scheduled global checks of all channels
- `global_check_schedule.frequency` - Schedule frequency ('daily' or 'monthly')
- `global_check_schedule.hour` - Hour to run check (0-23)
- `global_check_schedule.minute` - Minute to run check (0-59)
- `queue.check_on_update` - Automatically queue channels for checking when M3U playlists are updated
- `queue.max_channels_per_run` - Maximum number of channels to check per run
- `scoring.weights` - Weights for different quality factors in stream scoring

### Get Progress
```
GET /api/stream-checker/progress
```
Returns real-time progress of current check operation.

### Check Channel
```
POST /api/stream-checker/check-channel
Content-Type: application/json

{
  "channel_id": 123
}
```
Immediately checks a specific channel.

### Mark Updated
```
POST /api/stream-checker/mark-updated
Content-Type: application/json

{
  "channel_ids": [1, 2, 3]
}
```
Marks channels as updated and needing check.

## Automation Endpoints

### Get Automation Status
```
GET /api/automation/status
```
Returns automation service status and configuration.

### Start Automation
```
POST /api/automation/start
```
Starts the automation service.

### Stop Automation
```
POST /api/automation/stop
```
Stops the automation service.

### Get Configuration
```
GET /api/automation/config
```
Returns automation configuration.

### Update Configuration
```
PUT /api/automation/config
Content-Type: application/json

{
  "playlist_update_interval_minutes": 5,
  "autostart_automation": false,
  "enabled_m3u_accounts": [],
  "enabled_features": {
    "auto_playlist_update": true,
    "auto_stream_discovery": true,
    "changelog_tracking": true
  }
}
```
Updates automation configuration.

**Configuration Options:**
- `playlist_update_interval_minutes` - How often to check for playlist updates
- `autostart_automation` - Whether to automatically start the automation service on server startup
- `enabled_m3u_accounts` - Array of M3U account IDs to enable (empty array means all accounts)
- `enabled_features.auto_playlist_update` - Enable automatic playlist updates
- `enabled_features.auto_stream_discovery` - Enable automatic stream discovery via regex
- `enabled_features.changelog_tracking` - Track changes in the changelog

### Discover Streams
```
POST /api/automation/discover-streams
```
Manually triggers stream discovery cycle.

## Channel Endpoints

### Get Channels
```
GET /api/channels
```
Returns list of all channels.

**Query Parameters:**
- `page` - Page number (default: 1)
- `per_page` - Results per page (default: 50)

### Get Channel Details
```
GET /api/channels/{channel_id}
```
Returns details for a specific channel.

### Get Channel Streams
```
GET /api/channels/{channel_id}/streams
```
Returns all streams for a specific channel.

## Regex Pattern Endpoints

### Get Patterns
```
GET /api/regex-patterns
```
Returns all configured regex patterns.

### Add Pattern
```
POST /api/regex-patterns
Content-Type: application/json

{
  "pattern": "^HD.*Sports$",
  "channel_id": 123,
  "enabled": true
}
```
Adds a new regex pattern.

### Update Pattern
```
PUT /api/regex-patterns/{pattern_id}
Content-Type: application/json

{
  "pattern": "^HD.*Sports$",
  "channel_id": 123,
  "enabled": true
}
```
Updates an existing pattern.

### Delete Pattern
```
DELETE /api/regex-patterns/{pattern_id}
```
Deletes a regex pattern.

### Test Pattern
```
POST /api/regex-patterns/test
Content-Type: application/json

{
  "pattern": "^HD.*Sports$"
}
```
Tests a regex pattern against available streams.

## Changelog Endpoints

### Get Changelog
```
GET /api/changelog
```
Returns activity history.

**Query Parameters:**
- `start_date` - Filter by start date (ISO format)
- `end_date` - Filter by end date (ISO format)
- `page` - Page number
- `per_page` - Results per page

### Clear Changelog
```
POST /api/changelog/clear
```
Clears the activity history.

## Health Check

### Health Status
```
GET /api/health
```
Returns service health status.

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "services": {
    "automation": "running",
    "stream_checker": "running"
  }
}
```

## Unified Data Index Endpoints

The Unified Data Index (UDI) is a SQLite-based local database that caches all Dispatcharr data.
It is rebuilt on every M3U refresh and serves as the single source of truth for all stream operations.

### Get Index Status
```
GET /api/index/status
```
Returns UDI status and statistics.

**Response:**
```json
{
  "available": true,
  "stats": {
    "accounts": 5,
    "groups": 10,
    "channels": 150,
    "streams": 5000,
    "channel_streams": 8000,
    "pending_changes": 0,
    "last_sync": "2024-01-15T10:30:00"
  }
}
```

### Rebuild Index
```
POST /api/index/rebuild
```
Manually trigger a rebuild of the UDI from Dispatcharr.

**Response:**
```json
{
  "message": "Index rebuilt successfully",
  "counts": {
    "accounts": 5,
    "groups": 10,
    "channels": 150,
    "streams": 5000,
    "channel_streams": 8000
  }
}
```

### Get Index Changelog
```
GET /api/index/changelog
```
Returns changelog entries from the UDI.

**Query Parameters:**
- `days` - Number of days to include (default: 7)
- `limit` - Maximum entries to return (default: 100)

**Response:**
```json
[
  {
    "id": 1,
    "timestamp": "2024-01-15T10:30:00",
    "action": "streams_reordered",
    "entity_type": "channel",
    "entity_id": 123,
    "entity_name": "ESPN HD",
    "details": "{\"old_count\": 5, \"new_count\": 6}",
    "source": "stream_checker"
  }
]
```

### Get Pending Changes
```
GET /api/index/pending-changes
```
Returns changes pending sync to Dispatcharr.

**Query Parameters:**
- `limit` - Maximum changes to return (default: 100)

**Response:**
```json
{
  "count": 5,
  "changes": [
    {
      "id": 1,
      "entity_type": "channel",
      "entity_id": 123,
      "operation": "update_streams",
      "created_at": "2024-01-15T10:30:00",
      "sync_status": "pending"
    }
  ]
}
```

## Dispatcharr Sync Service Endpoints

The Sync Service is responsible for all communication with Dispatcharr.
It reads pending changes from the UDI and batches API calls.

### Get Sync Status
```
GET /api/sync/status
```
Returns sync service status.

**Response:**
```json
{
  "running": true,
  "has_token": true,
  "base_url": "http://dispatcharr:8000",
  "index_stats": {
    "streams": 5000,
    "pending_changes": 3
  },
  "sync_interval": 5
}
```

### Start Sync Service
```
POST /api/sync/start
```
Starts the background sync service.

### Stop Sync Service
```
POST /api/sync/stop
```
Stops the background sync service.

### Trigger Sync
```
POST /api/sync/trigger
```
Manually trigger sync of pending changes.

**Response:**
```json
{
  "message": "Sync completed",
  "stats": {
    "total": 5,
    "synced": 5,
    "failed": 0
  }
}
```
