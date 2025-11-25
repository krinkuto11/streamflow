# Caching Architecture

This document describes the caching architecture in StreamFlow for Dispatcharr.

## Overview

StreamFlow uses a centralized caching system based on SQLite to minimize API calls
to Dispatcharr while maintaining data consistency.

## Architecture Components

### 1. Unified Data Index (UDI)

**File:** `unified_data_index.py`

The UDI is a SQLite database that serves as the single source of truth for all
stream, channel, and account data. It provides:

- **Local Storage**: All data from Dispatcharr is stored locally in SQLite
- **Fast Queries**: Tools can query the local database instead of making API calls
- **Change Tracking**: Modifications are tracked in a `pending_changes` table
- **Changelog**: A permanent record of all changes is maintained

#### Database Schema

```
m3u_accounts      - M3U account information
channel_groups    - Channel groupings
channels          - Channel metadata
streams           - Stream metadata and stats
channel_streams   - Channel-stream relationships with ordering
pending_changes   - Changes waiting to be synced to Dispatcharr
changelog         - Permanent record of all changes
index_metadata    - Sync timestamps and configuration
```

### 2. Dispatcharr Sync Service

**File:** `dispatcharr_sync_service.py`

The Sync Service is the ONLY component that makes POST and PATCH requests to
Dispatcharr. This centralization provides:

- **Reduced API Calls**: Changes are batched before sending
- **Single Point of Communication**: All API communication goes through one service
- **Error Handling**: Failed syncs are tracked and can be retried
- **Background Processing**: Runs in a background thread

### 3. Dispatcharr Cache (Legacy Facade)

**File:** `dispatcharr_cache.py`

The cache module now acts as a facade over the UDI. It:

- Checks UDI first for any requested data
- Falls back to legacy in-memory caching if UDI is unavailable
- Maintains backward compatibility with existing code

## Data Flow

### M3U Refresh Flow

```
1. M3U Refresh triggered (automated or manual)
   |
2. Dispatcharr reloads M3U playlists
   |
3. Cache.rebuild_index() called
   |
4. DispatcharrSyncService.rebuild_index():
   - Fetches all accounts, groups, channels, streams from Dispatcharr
   - Rebuilds the UDI with fresh data
   - Creates changelog entry for rebuild
   |
5. Stream matching and checking tools now use UDI data
```

### Stream Checking Flow

```
1. Stream Checker analyzes channel streams
   |
2. Reads stream data from UDI (no API call)
   |
3. Updates stream stats in UDI
   |
4. Records pending change for stream stats update
   |
5. Reorders channel streams in UDI
   |
6. Records pending change for channel stream update
   |
7. Sync Service picks up pending changes
   |
8. Batches and sends to Dispatcharr
```

### Stream Matching Flow

```
1. Automated Stream Manager matches streams to channels
   |
2. Reads available streams from UDI
   |
3. Applies regex patterns to match
   |
4. Updates channel-stream relationships in UDI
   |
5. Records pending change
   |
6. Sync Service syncs to Dispatcharr
```

## Benefits

1. **Reduced API Calls**: Local SQLite database eliminates most read operations
2. **Single Source of Truth**: All tools use the same data source
3. **Batch Processing**: Changes are accumulated and sent together
4. **Resilience**: Works offline with local data, syncs when possible
5. **Auditability**: Complete changelog of all modifications

## API Endpoints

### Index Management

- `GET /api/index/status` - Get UDI status and statistics
- `POST /api/index/rebuild` - Manually trigger index rebuild
- `GET /api/index/changelog` - Get changelog entries
- `GET /api/index/pending-changes` - View pending changes

### Sync Service

- `GET /api/sync/status` - Get sync service status
- `POST /api/sync/start` - Start background sync
- `POST /api/sync/stop` - Stop background sync
- `POST /api/sync/trigger` - Manually trigger sync

## Configuration

The UDI is stored in the config directory:

```
$CONFIG_DIR/unified_index.db  - SQLite database file
```

Default: `/app/data/unified_index.db`

## Migration Notes

The new caching system is backward compatible with existing code:

1. The `dispatcharr_cache.py` module maintains its existing API
2. Tools using the cache will automatically use UDI when available
3. Falls back to legacy behavior if UDI is not available
