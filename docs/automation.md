# Automation

## How it works

StreamFlow runs a three-step pipeline on each channel according to its assigned schedule:

1. **M3U Update** — Refresh playlists from configured M3U providers in Dispatcharr
2. **Stream Matching** — Assign streams from the refreshed playlists to the channel via regex / TVG-ID
3. **Stream Checking** — Test each stream with ffmpeg, score by quality, reorder

Each step is independently enabled inside an **Automation Profile**. Which profile applies to a channel, and when it runs, is controlled by **Automation Periods**.

A channel only participates in automation if it has at least one period assigned. Channels without a period assignment are never touched.

---

## Automation Profiles

Profiles live in `automation_config.json` under `profiles`. A profile has three sections:

### `m3u_update`

```json
{
  "enabled": true,
  "playlists": []
}
```

- `enabled` — run the M3U refresh step
- `playlists` — list of playlist IDs to update (empty = all)

### `stream_matching`

```json
{
  "enabled": true,
  "validate_existing_streams": false
}
```

- `enabled` — run the stream matching step
- `validate_existing_streams` — when `true`, remove streams currently assigned to the channel if they no longer match any of the channel's patterns

### `stream_checking`

```json
{
  "enabled": true,
  "allow_revive": false,
  "check_all_streams": false,
  "stream_limit": 0,
  "min_resolution": null,
  "min_fps": 0,
  "min_bitrate": 0,
  "require_hdr": "any",
  "blank_check_enabled": false,
  "m3u_priority": [],
  "m3u_priority_mode": "absolute",
  "grace_period": false
}
```

| Field               | Description                                                           |
| ------------------- | --------------------------------------------------------------------- |
| `allow_revive`      | Re-add streams that were previously marked dead if they pass checking |
| `check_all_streams` | Check every stream on the channel, not just the top one               |
| `stream_limit`      | Max streams to check per channel (0 = no limit)                       |
| `min_resolution`    | Discard streams below this resolution (e.g. `"720p"`)                 |
| `min_fps`           | Discard streams below this FPS                                        |
| `min_bitrate`       | Discard streams below this bitrate (kbps)                             |
| `require_hdr`       | `"any"` / `"hdr"` / `"sdr"`                                           |
| `blank_check_enabled` | Run blank-screen detection during the quality probe                 |
| `m3u_priority`      | Ordered list of M3U account IDs — higher index = lower priority       |
| `m3u_priority_mode` | `"absolute"` (strict ordering) or `"equal"` (quality score only)      |
| `grace_period`      | If `true`, skip recently-checked streams within the grace window      |

### `scoring_weights`

Controls how quality dimensions are weighted when scoring streams. Values should sum to 1.0.

```json
{
  "bitrate": 0.35,
  "resolution": 0.30,
  "fps": 0.15,
  "codec": 0.10,
  "hdr": 0.10,
  "prefer_h265": true
}
```

---

## Automation Periods

Periods define **when** to run. They have no profile attached; the profile is specified per-channel when assigning.

```json
{
  "id": "...",
  "name": "Every 2 hours",
  "schedule": {
    "type": "interval",
    "value": 120
  }
}
```

### Schedule types

**Interval** — runs every N minutes:

```json
{ "type": "interval", "value": 120 }
```

**Cron** — runs on a cron expression:

```json
{ "type": "cron", "value": "0 */4 * * *" }
```

---

## Channel Period Assignments

Each channel can have multiple periods, each with a different profile:

```json
"channel_period_assignments": {
  "42": {
    "<period-id-1>": "<profile-id-a>",
    "<period-id-2>": "<profile-id-b>"
  }
}
```

Configured from the UI under **Scheduling → Channels**.

---

## EPG Scheduling

StreamFlow can pull EPG data from Dispatcharr and create one-time scheduled checks triggered by program start times. Configured in the Scheduling page under **Auto-Create Rules**.

EPG data is refreshed at the interval configured in the scheduling service (`epg_refresh_interval_minutes`, default 60 minutes).

---

## Global Action

Triggers an immediate full cycle (M3U update → match → check) across all channels, bypassing the grace period. Available as a button in the Stream Checker page, and can also be scheduled via cron.
