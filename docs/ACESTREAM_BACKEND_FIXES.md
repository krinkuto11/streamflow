# AceStream Monitor Backend Fixes - Summary

## Problem Statement
The AceStream Monitor service was experiencing multiple errors when attempting to change settings:

### Errors Fixed
1. **Error**: `'DispatcharrConfig' object does not support item assignment`
   - **Location**: `web_api.py:3594` in `update_acestream_config()`
   - **Cause**: Attempting to use `config['acestream_enabled'] = data['enabled']`

2. **Error**: `'UDIManager' object has no attribute 'get_channel'`
   - **Location**: `web_api.py:3669` in `tag_channel_as_acestream()`
   - **Cause**: Using incorrect method name `get_channel()` instead of `get_channel_by_id()`

3. **Error**: `'UDIManager' object has no attribute 'get_all_channels'`
   - **Location**: `acestream_monitor_service.py:81` in `_get_acestream_channels()`
   - **Location**: `web_api.py:3631` in `get_acestream_channels()`
   - **Cause**: Using incorrect method name `get_all_channels()` instead of `get_channels()`

4. **Error**: `'DispatcharrConfig' object has no attribute 'get'`
   - **Location**: `web_api.py:3558` in `get_acestream_config()`
   - **Cause**: Attempting to use `config.get('acestream_enabled', False)`

## Solutions Implemented

### 1. DispatcharrConfig Enhancements
**File**: `backend/dispatcharr_config.py`

Added the following methods to support AceStream configuration:

```python
def get(self, key: str, default: Any = None) -> Any:
    """Get a configuration value by key with optional default."""
    with self._lock:
        return self._config.get(key, default)

def __getitem__(self, key: str) -> Any:
    """Get a configuration value using dictionary syntax."""
    with self._lock:
        return self._config[key]

def __setitem__(self, key: str, value: Any) -> None:
    """Set a configuration value using dictionary syntax."""
    with self._lock:
        self._config[key] = value

def save(self) -> bool:
    """Save configuration to file."""
    return self._save_config()
```

**Benefits**:
- Enables storing AceStream configuration alongside Dispatcharr credentials
- Supports both dictionary-style access (`config['key']`) and method calls (`config.get('key')`)
- Thread-safe implementation with locking
- Persistent storage via JSON file

### 2. UDIManager Method Corrections
**Files**: `backend/acestream_monitor_service.py`, `backend/web_api.py`

Updated all incorrect method calls:

| Incorrect Method | Correct Method | Description |
|-----------------|----------------|-------------|
| `get_all_channels()` | `get_channels()` | Retrieve all channels |
| `get_channel(id)` | `get_channel_by_id(id)` | Get specific channel |
| `get_stream(id)` | `get_stream_by_id(id)` | Get specific stream |

### 3. Object to Dictionary Access Pattern
**Files**: `backend/acestream_monitor_service.py`, `backend/web_api.py`

Changed from object attribute access to dictionary access:

| Old Pattern | New Pattern | Example |
|------------|-------------|---------|
| `channel.id` | `channel.get('id')` | Get channel ID |
| `channel.name` | `channel.get('name')` | Get channel name |
| `stream.url` | `stream.get('url')` | Get stream URL |
| `getattr(ch, 'is_acestream', False)` | `ch.get('is_acestream', False)` | Check if AceStream |
| `channel.to_dict()` | *(removed)* | Already a dict |

**Rationale**: The UDIManager returns dictionaries, not objects, so we need to use dictionary access patterns throughout.

### 4. Added Defensive Null Checks
**File**: `backend/acestream_monitor_service.py`

Added null checks in `_reorder_streams_by_health()` to prevent AttributeError:

```python
if not channel:
    logger.error("Cannot reorder streams: channel is None")
    return

channel_id = channel.get('id')
if not channel_id:
    logger.error("Cannot reorder streams: channel has no ID")
    return
```

## Testing

### Unit Tests
Created `backend/tests/test_acestream_monitor_fixes.py` with comprehensive tests:
- DispatcharrConfig dictionary-style access
- DispatcharrConfig get() method
- DispatcharrConfig save() method
- AceStream configuration storage
- Channel dictionary access patterns
- Stream dictionary access patterns

**Result**: All 6 tests passing ✓

### Verification Script
Created `backend/tests/verify_acestream_fixes.py` to manually verify all fixes:
- DispatcharrConfig item assignment
- DispatcharrConfig get() method
- UDIManager channel access
- Stream dictionary access

**Result**: All verifications passing ✓

### Security Analysis
Ran CodeQL security scanner on all changes.

**Result**: 0 alerts, no security issues ✓

### Syntax Validation
Validated Python syntax on all modified files.

**Result**: No syntax errors ✓

## Files Changed

### Modified Files (3)
1. `backend/dispatcharr_config.py` - Added dictionary-style access methods
2. `backend/acestream_monitor_service.py` - Fixed UDI method calls and object access
3. `backend/web_api.py` - Fixed UDI method calls and object access

### New Test Files (2)
1. `backend/tests/test_acestream_monitor_fixes.py` - Unit tests
2. `backend/tests/verify_acestream_fixes.py` - Verification script

## Impact

### Before
- ❌ Cannot update AceStream configuration via API
- ❌ Cannot tag channels as AceStream channels
- ❌ Cannot retrieve AceStream channels
- ❌ AceStream monitoring service fails to start
- ❌ Multiple runtime errors in logs

### After
- ✅ AceStream configuration can be updated via API
- ✅ Channels can be tagged as AceStream channels
- ✅ AceStream channels can be retrieved
- ✅ AceStream monitoring service can start successfully
- ✅ No runtime errors related to these issues

## API Endpoints Now Working

### GET `/api/acestream/config`
Retrieves AceStream configuration:
```json
{
  "enabled": true,
  "orchestrator_url": "http://gluetun:19000",
  "monitoring_interval": 30,
  "ffmpeg_probe_duration": 5
}
```

### POST `/api/acestream/config`
Updates AceStream configuration:
```json
{
  "enabled": true,
  "orchestrator_url": "http://gluetun:19000",
  "monitoring_interval": 30,
  "ffmpeg_probe_duration": 5
}
```

### GET `/api/acestream/channels`
Retrieves all AceStream-tagged channels

### POST `/api/acestream/channels/{channel_id}/tag`
Tags a channel as AceStream:
```json
{
  "is_acestream": true,
  "orchestrator_url": "http://gluetun:19000"
}
```

## Backward Compatibility

All changes are backward compatible:
- DispatcharrConfig still supports all existing methods
- Added methods enhance functionality without breaking existing code
- UDIManager API unchanged, only usage corrected

## Code Quality

- ✅ No syntax errors
- ✅ No security vulnerabilities (CodeQL)
- ✅ All tests passing
- ✅ Defensive null checks added
- ✅ Thread-safe implementation
- ✅ Consistent error handling
- ✅ Proper logging throughout

## Frontend UI Refresh Fix (January 2026)

### Problem
The AceStream Monitoring page showed stream health data (alive/dead counts and stream details) only when clicking the Reload button. When the 30-second monitoring interval kicked in, the stats would reset to "0 alive, 0 dead" with no stream details visible.

### Root Cause
**File**: `frontend/src/pages/AceStreamMonitoring.jsx`

The interval refresh (lines 44-47) only called:
- `loadStatus()`
- `loadChannelStreamsHealth()`

It did NOT call `loadAceStreamChannels()` to refresh the `channels` state. Since `loadChannelStreamsHealth()` depends on the `channels` state to iterate over channels and fetch stream health data, when the interval ran with a stale/empty `channels` array, no health data was loaded.

### Solution
Updated the interval refresh to:
1. Call `loadAceStreamChannels()` to refresh the channels state
2. Then call `loadChannelStreamsHealth()` to load health data
3. Use proper async/await pattern with error handling
4. Add a guard flag to prevent overlapping executions

```javascript
useEffect(() => {
  loadData()
  let isRefreshing = false
  const interval = setInterval(async () => {
    if (isRefreshing) {
      return // Skip if previous refresh is still running
    }
    try {
      isRefreshing = true
      await loadStatus()
      await loadAceStreamChannels()
      await loadChannelStreamsHealth()
    } catch (error) {
      console.error('Error during interval refresh:', error)
    } finally {
      isRefreshing = false
    }
  }, 30000) // Refresh every 30 seconds
  return () => clearInterval(interval)
}, [])
```

### Impact
- ✅ Stream health data now persists across interval refreshes
- ✅ Alive/dead counts update correctly every 30 seconds
- ✅ Stream details remain visible in dropdown menus
- ✅ Proper error handling prevents silent failures
- ✅ Guard flag prevents overlapping API calls

### Testing
- ✅ Frontend builds successfully
- ✅ No syntax errors
- ✅ No security vulnerabilities (CodeQL)
- ✅ Code review completed

## Conclusion

All four backend errors have been successfully fixed. The AceStream Monitor service is now fully functional and ready for use. The frontend UI refresh issue has also been resolved, ensuring consistent data display.
