# StreamFlow Operations Guide

This guide explains how to operate StreamFlow after installation. It focuses on
safe defaults, automation profiles, quality checks, monitoring, and recovery
behavior.

## Operating Model

StreamFlow automates Dispatcharr channel maintenance through small, explicit
steps. A full automation run can refresh M3U playlists, match streams to
channels, queue channels for checking, analyze stream quality, and write the
best stream order back to Dispatcharr.

Each step is controlled by an Automation Profile. Keep profiles narrow:

- Use a full profile when you want playlist refresh, matching, and checking.
- Use a quality-only profile when streams are already assigned and only scoring
  or reordering is needed.
- Use a conservative profile for event or short-window checks.
- Disable destructive actions unless the run is expected to enforce them.

The Dashboard shows the active automation stage, recent activity, queue state,
stream checker state, and Shadow Monitor state. During long runs, prefer the
Dashboard over log tailing for normal progress and use logs only for detailed
debugging.

## Authentication

StreamFlow can connect to Dispatcharr with either an API key or legacy
username/password credentials.

Recommended setup:

1. Create or copy a Dispatcharr API key for StreamFlow.
2. Enter the Dispatcharr URL and API key in StreamFlow settings.
3. Run the connectivity check.
4. Keep username/password credentials only for installations that cannot use API
   keys yet.

API key authentication avoids storing a password and is the preferred mode for
new installs. Existing username/password installs remain supported so updates do
not break older configurations.

## Automation Profiles

Profiles are the main safety boundary. Review these profile sections before
running automation:

| Section | Purpose | Safe default |
| --- | --- | --- |
| M3U update | Refresh playlist sources | Enabled only for full runs |
| Stream matching | Assign streams to channels | Enabled only when patterns should be applied |
| Stream checking | Score, reorder, and optionally remove bad streams | Enabled for quality runs |
| Dead stream handling | Mark or remove failed streams | Removal disabled until confidence is high |
| Blank/freeze detection | Detect black or frozen video | Optional and profile-specific |
| Scoring weights | Rank streams by quality | Defaults are safe for most installs |

Use quality-only profiles for targeted validation because they avoid refreshing
playlists or changing regex/matching state.

## Run Stages

An automation run moves through these stages:

1. Preparing
2. Schedule
3. M3U refresh
4. Cache sync
5. Matching
6. Queueing
7. Quality check
8. Finalizing

Skipped stages should be shown as skipped rather than silent zeros. If a full run
does not refresh M3U or perform matching, check that the selected profile has
those stages enabled.

## Quality Checks

Quality checks use ffmpeg to collect bitrate, resolution, FPS, codec, HDR, and
optional blank/freeze signals. StreamFlow then scores streams and reorders the
channel.

Important settings:

| Setting | Effect |
| --- | --- |
| FFmpeg duration | Probe window length per stream |
| Timeout | Base stream operation timeout |
| Retry attempts | Retry count for failed stream probes |
| Retry delay | Delay between retry attempts |
| Stream limit | Maximum streams checked per channel |
| Check all streams | Check every stream instead of only the active one |
| Start selection | Choose where the next queue starts |

The start selection setting controls the first channel of the next run. Use
wording like "Next run starts at" when reviewing UI state because profile names
vary between installations.

## Provider Limits

Per-provider or per-account limits prevent StreamFlow from opening too many
streams on the same source at once.

When a provider is saturated, StreamFlow should defer streams from that provider
and continue checking streams from providers with available capacity. If all
candidate streams are blocked by provider limits, StreamFlow waits up to the
configured provider wait timeout. A provider-limit timeout skips that stream for
the current run without marking it dead or removing it from the channel.

Expected operator behavior:

- Waiting streams are normal when a provider limit is full.
- A provider-limit skip is not a dead-stream signal.
- If many streams are skipped by provider limits, increase the wait timeout or
  run checks during a quieter period.

## Connectivity Guard

The connectivity guard protects destructive quality-check steps. Before marking
streams dead or writing reordered streams, StreamFlow checks that required
network/API targets are reachable.

If the guard fails:

1. The quality step aborts safely.
2. Existing channel assignments are left unchanged.
3. The Dashboard and changelog should show the failure reason.
4. A later successful stale-recovery check clears old guard failures.

Use retry count, retry delay, timeout, and stale-recheck interval settings to
fit your environment. Do not disable the guard unless another layer provides the
same protection.

## Dead, Blank, And Freeze Detection

Dead-stream tracking records streams that fail quality analysis. Depending on
profile settings, dead streams can be excluded from scoring, revived later, or
removed from a channel.

Blank and freeze detection are optional. They are useful for streams that stay
connected while showing black video or a frozen frame. Use them carefully for
event channels because event feeds can show placeholders before the program
starts.

Recommended event behavior:

- Early preflight: score/reorder only.
- Near start time: optionally enable blank/freeze/dead enforcement.
- Keep removal optional unless you trust the source timing.

## Shadow Monitor

Shadow Monitor watches active channels from the viewer side and can trigger a
stream switch when a watched channel appears blank or frozen.

Recommended setup:

1. Create a dedicated Dispatcharr user or API key for Shadow Monitor.
2. Do not reuse the main administrator identity for watcher traffic.
3. Configure the watcher API key in the Shadow Monitor page.
4. Start with dry run enabled.
5. Confirm that watched channels and recent events look correct.
6. Disable dry run only after the decisions are safe.

The watcher identity should be separate because Shadow Monitor creates its own
viewing sessions. A dedicated identity makes it clear which connections are real
clients and which ones belong to StreamFlow.

Single-stream channels cannot be switched to another stream. Shadow Monitor
should record that decision clearly instead of retrying a switch that cannot
succeed.

## Teamarr Event Preflight

Teamarr event preflight is optional and disabled by default. It polls Teamarr
managed channels, finds upcoming events, and starts a targeted StreamFlow quality
check for the Dispatcharr channel Teamarr already created.

Key rules:

- Teamarr remains the source of truth for event channel matching.
- StreamFlow should not run regex matching for the event preflight path.
- The connector needs the Teamarr base URL. Current Teamarr APIs are expected
  to be reachable without a StreamFlow-side API key.
- Include/exclude sport and league filters can restrict which managed events are
  preflighted.
- The safest default is scoring/reorder only.
- Teamarr event checks use high waiting priority, but priority only sorts work
  that is still waiting. It does not stop the channel currently being checked.

StreamFlow creates a `Teamarr Event Preflight` automation profile if it is
missing. That default profile is intentionally conservative: no playlist
refresh, no regex matching, no automatic dead-stream removal, and stream
checking enabled for scoring current event-channel streams.

To customize event behavior, create or edit an Automation Profile, then select
it on the Teamarr Preflight page and save. The selected profile controls whether
the preflight only scores/reorders streams or also runs stricter checks such as
dead, blank, or freeze handling.

`Teamarr API Poll Interval` controls how often StreamFlow reads Teamarr managed
event state. Use 30-60 seconds for normal event automation; longer intervals can
miss narrow windows such as a 1-minute preflight offset or a short post-start
grace.

`Preflight Offset` is the main automatic check before event start. `Pre-Start
Retries` are one or more minute offsets before start, not a retry count. A
single value such as `3` is valid; multiple values such as `10,3` create
multiple extra buckets. For example, run a safe preflight 20 minutes before
start and retry 10 minutes and 3 minutes before start if the channel was not
ready.

`Post-Start Checks` are one or more minute offsets after start for providers
that publish or rename event channels at kickoff. A single value such as `2` is
valid; multiple values such as `2,4` create multiple post-start buckets. The
default post-start checks are 2 minutes and 4 minutes after start; keep
post-start grace at least as large as the largest post-start offset and wide
enough for the poll interval. If the Stream Checker is already busy, the event
check is queued ahead of lower-priority
waiting work and runs after the active channel finishes.

## Hardware Acceleration

Hardware acceleration is optional and disabled by default. CPU probing remains
the default behavior for compatibility.

Supported modes depend on the ffmpeg build and container runtime. StreamFlow can
save a preferred mode, optional device path, and CPU fallback preference from the
Stream Checker configuration.

Recommended setup:

1. Leave hardware acceleration disabled after install.
2. Confirm normal CPU checks work.
3. Expose the required GPU runtime/devices to the container.
4. Enable hardware acceleration with CPU fallback still enabled.
5. Run a short targeted quality check.
6. Check logs for fallback warnings or device initialization errors.

If hardware initialization fails and fallback is enabled, StreamFlow should retry
the analysis on CPU. If fallback is disabled, unavailable hardware can make the
stream check fail.

The same hardware acceleration setting is passed into the ffmpeg probes used for
stream analysis, blank detection, freeze detection, and loop probing. Some
detection filters still run as software filters, but hardware decode can be used
when ffmpeg supports the selected mode. CPU fallback only retries failures that
look like hardware/device initialization errors; normal dead streams, HTTP
failures, and timeouts are still handled as stream results.

### Container Template Notes

When using a GUI-managed container template or app template, keep the StreamFlow
container managed through that template so future edits stay visible and
reversible in the host UI. Do not make GPU or path changes only through an
ad-hoc `docker run` command and then treat that as the finished install.

Recommended template-managed flow:

1. Keep the image repository/tag in the template.
2. Keep `/app/data` mapped to persistent application storage.
3. Add GPU runtime settings, device mappings, or environment variables through
   template fields so they remain GUI-editable.
4. For NVIDIA passthrough, expose the NVIDIA runtime or devices supported by the
   host and set `NVIDIA_VISIBLE_DEVICES` plus
   `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video` when required by the host
   plugin/runtime.
5. Start in CPU mode or Auto mode with CPU fallback enabled.
6. Run a short targeted quality check, then review Stream Checker hardware
   status before using hardware decode for larger checks.

The Stream Checker page should show the effective analysis path. The hardware
status API is also useful for remote checks: it reports the configured mode,
fallback setting, detected ffmpeg methods, GPU visibility, and whether
StreamFlow currently expects CPU-only, hardware-preferred, or hardware-only
probing.

If a template definition is updated by hand, take a timestamped backup first and
keep only the latest few backups. This makes it easy to roll back while avoiding
old template copies piling up.

### Docker Compose Examples

Use the CPU-only compose shape for ordinary servers or for validating that
StreamFlow starts without GPU passthrough:

```yaml
services:
  streamflow:
    image: ghcr.io/krinkuto11/streamflow:latest
    container_name: streamflow
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /srv/streamflow/data:/app/data
    environment:
      TZ: Europe/Berlin
      CONFIG_DIR: /app/data
```

For NVIDIA/CUDA probing on a normal Docker Compose host, install the NVIDIA
Container Toolkit on the host first, then expose the GPU runtime to StreamFlow:

```yaml
services:
  streamflow:
    image: ghcr.io/krinkuto11/streamflow:latest
    container_name: streamflow
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /srv/streamflow/data:/app/data
    environment:
      TZ: Europe/Berlin
      CONFIG_DIR: /app/data
      NVIDIA_VISIBLE_DEVICES: all
      NVIDIA_DRIVER_CAPABILITIES: compute,utility,video
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities:
                - gpu
```

Some non-Swarm Compose installations ignore `deploy.resources`. In that case,
use the runtime/device syntax supported by your Docker version, for example:

```yaml
services:
  streamflow:
    image: ghcr.io/krinkuto11/streamflow:latest
    container_name: streamflow
    restart: unless-stopped
    runtime: nvidia
    ports:
      - "5000:5000"
    volumes:
      - /srv/streamflow/data:/app/data
    environment:
      TZ: Europe/Berlin
      CONFIG_DIR: /app/data
      NVIDIA_VISIBLE_DEVICES: all
      NVIDIA_DRIVER_CAPABILITIES: compute,utility,video
```

For Intel/DRI probing, pass the host DRI devices into the container and keep CPU
fallback enabled while testing. Set `RENDER_GID` to the group ID that owns the
host render device, often visible with `ls -l /dev/dri/renderD128`:

```yaml
services:
  streamflow:
    image: ghcr.io/krinkuto11/streamflow:latest
    container_name: streamflow
    restart: unless-stopped
    ports:
      - "5000:5000"
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "${RENDER_GID:-109}"
    volumes:
      - /srv/streamflow/data:/app/data
    environment:
      API_HOST: 0.0.0.0
      API_PORT: 5000
      TZ: Europe/Berlin
      CONFIG_DIR: /app/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

If you change `API_PORT`, keep the port mapping and healthcheck URL in sync. For
example, `API_PORT: 4919` should use a `4919:4919` mapping and
`http://localhost:4919/api/health` in the healthcheck.

After starting a hardware template, open StreamFlow, keep CPU fallback enabled,
and run a targeted quality check before enabling GPU decode for large full
checks. The hardware status API and startup logs should show whether CUDA or
DRI/VAAPI/QSV methods are available, or whether StreamFlow is safely using CPU
fallback.

## Dashboard, Changelog, And Logs

Use the Dashboard for live state:

- current automation stage
- queue progress
- stream checker status
- Shadow Monitor watched-channel status
- last update time

Use Changelog for completed actions:

- automation runs
- playlist refreshes
- matching runs
- batch stream checks
- single-channel checks
- stream assignment changes

Use backend logs when the UI shows an error, an abort, or unclear waiting state.
Remaining ffmpeg warnings should be interpreted in context; a timeout on one
stream does not automatically mean the run failed.

## Troubleshooting

### Connectivity check failed

- Verify Dispatcharr URL and authentication.
- Prefer API key auth for new installs.
- Run the settings connectivity check again.
- If the guard failure is stale, wait for stale-recovery polling or trigger a
  fresh guarded check.

### Quality check is waiting

- Check provider-limit counters.
- If all streams are waiting on provider limits, increase provider wait timeout
  or reduce worker count.
- Waiting for provider capacity should not mark streams dead.

### A full run skipped M3U refresh or matching

- Check the profile used by the run.
- Confirm M3U update and matching are enabled in that profile.
- Review Dashboard stages for skipped state.

### Shadow Monitor sees no watched channels

- Confirm clients are actively playing through Dispatcharr.
- Confirm the watcher API key is saved.
- Make sure Shadow Monitor is enabled and not only in dry run unless intended.
- Check whether the active connection belongs only to the watcher identity.

### Hardware acceleration falls back to CPU

- Confirm the container has access to the required GPU devices/runtime.
- Confirm the selected ffmpeg mode is supported.
- Leave fallback enabled while testing.
- Use CPU mode if the runtime is not stable.

## Release Checklist

Before considering a StreamFlow change ready:

1. Feature disabled or update-safe by default.
2. Targeted unit tests pass.
3. Frontend build passes when UI changed.
4. Live verification exercised the real update path.
5. Logs contain no unexplained Traceback, ERROR, or CRITICAL entries.
6. Screenshots are sanitized and dark mode where practical.
7. PR text explains the problem, implementation, tests, screenshots, and Live
   Verification without environment-specific details.
