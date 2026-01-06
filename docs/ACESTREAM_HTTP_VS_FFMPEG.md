# AceStream Monitoring - HTTP vs FFmpeg Methods

## Overview

StreamFlow now supports two methods for keeping AceStream streams alive in the orchestrator:

1. **HTTP Method (Default)** - Lightweight HTTP range requests
2. **FFmpeg Method** - Traditional continuous ffmpeg processes

## Why Two Methods?

The original implementation used continuous ffmpeg processes to consume stream data, keeping the AceStream orchestrator thinking there's an active player. While functional, this approach has some drawbacks:

- **Resource Intensive**: Each ffmpeg process consumes CPU and memory
- **Overhead**: Running many concurrent ffmpeg processes can strain the system
- **Complexity**: Managing process lifecycle and output parsing

The new HTTP method provides the same functionality with significantly lower resource usage.

## HTTP Method (Recommended)

### How It Works

The HTTP method makes periodic HTTP range requests to the stream URL, requesting small chunks of data (default: 64KB every 10 seconds). This:

1. Simulates a player consuming data
2. Keeps the stream registered in the orchestrator's `/streams` endpoint
3. Detects dead/broken streams (EOF, connection errors, timeouts)
4. Uses minimal resources

### Advantages

- ✅ **Low Resource Usage**: No ffmpeg processes needed
- ✅ **Lightweight**: Only requests small chunks periodically
- ✅ **Dead Stream Detection**: Detects EOF, connection errors, and timeouts
- ✅ **Configurable**: Adjustable interval and chunk size
- ✅ **Same Functionality**: Keeps streams alive just like ffmpeg

### Configuration

```python
config = {
    'monitoring_method': 'http',  # Use HTTP method
    'http_keepalive_interval': 10,  # Seconds between requests
    'http_chunk_size': 65536,  # 64KB per request
}
```

### How HTTP Keep-Alive Works

1. **Initial Request**: Makes HTTP range request for first 64KB
2. **Periodic Requests**: Every 10s, requests next 64KB chunk
3. **Health Tracking**: Monitors response status, detects failures
4. **Dead Detection**: After 3 consecutive failures, marks stream as dead
5. **Retry Logic**: Dead streams are retried after configured interval

### Health Scoring with HTTP Method

The HTTP method provides health metrics that contribute to the overall health score:

- **Stream Alive** (20 points): HTTP requests are successful
- **Low Failures** (0-15 points): Based on failure count
  - 0 failures: 15 points
  - 1 failure: 10 points
  - 2 failures: 5 points
- **Failure Penalty**: -5 points per failure beyond 2 (max -20)

Combined with orchestrator stats (peers, speeds), streams are scored 0-100 and automatically reordered.

## FFmpeg Method (Legacy)

### How It Works

The FFmpeg method runs continuous ffmpeg processes that consume stream data to `/dev/null`. This:

1. Keeps the stream alive in the orchestrator
2. Provides detailed stream statistics (codec, resolution, bitrate, FPS)
3. Detects stream errors through ffmpeg output

### Advantages

- ✅ **Detailed Stats**: Codec, resolution, bitrate, FPS information
- ✅ **Proven Method**: Original approach, well-tested
- ✅ **Stream Validation**: Verifies stream format and quality

### Disadvantages

- ❌ **Resource Intensive**: Each stream needs a continuous ffmpeg process
- ❌ **Higher CPU Usage**: Decoding stream data uses CPU
- ❌ **More Memory**: Multiple processes consume more memory
- ❌ **Complexity**: Process management and output parsing

### Configuration

```python
config = {
    'monitoring_method': 'ffmpeg',  # Use FFmpeg method
    'max_ffmpeg_failures': 3,  # Failures before marking dead
}
```

### Health Scoring with FFmpeg Method

The FFmpeg method provides detailed stream metrics:

- **Stream Working** (20 points): FFmpeg process is running
- **Bitrate Quality** (0-15 points): Higher bitrate = higher score (assumes 3000+ kbps is good)
- **Error Penalty**: -5 points per error in ffmpeg output (max -20)

## Comparison

| Feature | HTTP Method | FFmpeg Method |
|---------|-------------|---------------|
| Resource Usage | ⭐⭐⭐⭐⭐ Very Low | ⭐⭐ Moderate |
| CPU Usage | ⭐⭐⭐⭐⭐ Minimal | ⭐⭐ Higher |
| Memory Usage | ⭐⭐⭐⭐⭐ Minimal | ⭐⭐ Higher |
| Dead Stream Detection | ✅ Yes (EOF, errors) | ✅ Yes (EOF, errors) |
| Stream Quality Metrics | ❌ No codec/res/fps | ✅ Yes |
| Setup Complexity | ⭐⭐⭐⭐⭐ Simple | ⭐⭐⭐ Moderate |
| Scalability | ⭐⭐⭐⭐⭐ Excellent | ⭐⭐⭐ Good |
| Recommended | ✅ Yes (default) | For detailed metrics |

## Configuration Examples

### Default Configuration (HTTP)

```json
{
  "monitoring_method": "http",
  "monitoring_interval": 30,
  "http_keepalive_interval": 10,
  "http_chunk_size": 65536,
  "dead_stream_retry_interval": 300
}
```

### FFmpeg Configuration

```json
{
  "monitoring_method": "ffmpeg",
  "monitoring_interval": 30,
  "max_ffmpeg_failures": 3,
  "dead_stream_retry_interval": 300
}
```

### Optimized for Many Streams (HTTP)

```json
{
  "monitoring_method": "http",
  "monitoring_interval": 60,
  "http_keepalive_interval": 15,
  "http_chunk_size": 32768,
  "dead_stream_retry_interval": 600
}
```

## Dead Stream Detection

Both methods detect dead streams through:

1. **EOF Detection**: Empty response or stream end
2. **Connection Errors**: Network failures, timeouts
3. **HTTP Errors**: 404, 500, etc. (HTTP method)
4. **FFmpeg Errors**: Decoding errors, corrupt data (FFmpeg method)

After detecting a dead stream:
- Stream is marked as dead in the tracker
- Monitoring is stopped for that stream
- Stream is retried after configured interval (default: 5 minutes)

## Migration Guide

### Switching from FFmpeg to HTTP

1. Update configuration:
   ```python
   config['monitoring_method'] = 'http'
   config['http_keepalive_interval'] = 10
   config['http_chunk_size'] = 65536
   ```

2. Restart AceStream monitoring service

3. Existing FFmpeg processes will be stopped during shutdown

4. HTTP keep-alive will start automatically

### Switching from HTTP to FFmpeg

1. Ensure ffmpeg is installed in the container

2. Update configuration:
   ```python
   config['monitoring_method'] = 'ffmpeg'
   ```

3. Restart AceStream monitoring service

4. FFmpeg processes will start for each stream

## Troubleshooting

### HTTP Method Issues

**Streams not appearing in orchestrator:**
- Check if HTTP requests are successful (view logs)
- Verify stream URL is correct and accessible
- Ensure keepalive interval is not too long

**High failure rate:**
- Increase `http_keepalive_interval` to reduce request frequency
- Check network connectivity to stream source
- Verify orchestrator is running and accessible

### FFmpeg Method Issues

**High CPU usage:**
- Consider switching to HTTP method
- Reduce number of concurrent streams
- Check for inefficient stream formats

**FFmpeg not found:**
- Ensure ffmpeg is installed: `apt-get install ffmpeg`
- Verify ffmpeg is in PATH

## Performance Recommendations

### For Many Streams (10+)

Use HTTP method:
```json
{
  "monitoring_method": "http",
  "http_keepalive_interval": 15,
  "monitoring_interval": 60
}
```

### For Few Streams with Quality Metrics Needed

Use FFmpeg method:
```json
{
  "monitoring_method": "ffmpeg",
  "monitoring_interval": 30
}
```

### For Low-Resource Environments

Use HTTP method with longer intervals:
```json
{
  "monitoring_method": "http",
  "http_keepalive_interval": 20,
  "http_chunk_size": 32768,
  "monitoring_interval": 90
}
```

## Implementation Details

### HTTP Keep-Alive Loop

```python
while not stop_event.is_set():
    # Make HTTP range request
    headers = {'Range': f'bytes={position}-{position + chunk_size - 1}'}
    response = session.get(stream_url, headers=headers, timeout=10)
    
    # Check response
    if response.status_code in (200, 206):
        chunk = response.content
        if len(chunk) == 0:
            # EOF detected
            handle_failure('eof')
        else:
            # Success
            update_health(success=True)
            position += len(chunk)
    else:
        # HTTP error
        handle_failure('http_error')
    
    # Wait for next interval
    stop_event.wait(interval)
```

### Dead Stream Detection

Both methods track consecutive failures:

1. **Success**: Reset failure counter
2. **Failure**: Increment failure counter
3. **Max Failures Reached**: Mark as dead, stop monitoring
4. **Retry**: After retry interval, attempt restart

## Best Practices

1. **Use HTTP Method by Default**: Lower resource usage, simpler operation
2. **Monitor Resource Usage**: Check CPU/memory if using FFmpeg method
3. **Adjust Intervals**: Balance between responsiveness and resource usage
4. **Enable Logging**: Monitor for errors and dead stream detection
5. **Test Configuration**: Verify streams stay alive in orchestrator

## Future Enhancements

Possible improvements:

1. **Hybrid Mode**: Use HTTP for keep-alive, periodic FFmpeg for quality metrics
2. **Adaptive Intervals**: Adjust based on stream stability
3. **Bandwidth Limiting**: Configurable maximum bandwidth per stream
4. **Stats Collection**: Gather performance metrics for both methods
5. **Auto-Selection**: Automatically choose best method based on system resources

## Conclusion

The HTTP method is recommended for most use cases due to its significantly lower resource usage while providing the same core functionality as the FFmpeg method. Use FFmpeg only when you need detailed stream quality metrics (codec, resolution, bitrate, FPS) and have sufficient system resources.
