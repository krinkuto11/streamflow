# Stream Checking

## Overview

Stream checking is the third step of the automation pipeline. It uses ffmpeg to analyze each stream, scores based on quality dimensions, and reorders streams so the best one is at the top of the channel in Dispatcharr.

---

## Quality checking

Each stream is analyzed by spawning a short ffmpeg probe session. Extracted metrics:

- Bitrate (kbps)
- Resolution (width × height)
- FPS
- Codec (H.264, H.265/HEVC, AV1, etc.)
- HDR (detected from ffmpeg stderr: pixel format, color space, primaries, transfer function)
- Error presence (dropped frames, decode errors)

---

## Scoring

Streams are scored 0–100 using weighted dimensions. Weights are configured per Automation Profile under `scoring_weights`.

| Dimension  | Default weight |
| ---------- | -------------- |
| Bitrate    | 35%            |
| Resolution | 30%            |
| FPS        | 15%            |
| Codec      | 10%            |
| HDR        | 10%            |

**M3U source priority** is applied on top of the quality score. Priority values are 0–100 (higher = more preferred). Two modes:

- `absolute` — higher-priority source streams always rank above lower-priority streams regardless of quality score
- `equal` — quality score only, M3U account is ignored for ordering

---

## Filters

Before scoring, streams can be discarded based on minimum thresholds (set in `stream_checking` within the profile):

| Field            | Effect                                                      |
| ---------------- | ----------------------------------------------------------- |
| `min_resolution` | Skip streams below this resolution                          |
| `min_fps`        | Skip streams below this FPS                                 |
| `min_bitrate`    | Skip streams below this bitrate (kbps)                      |

---

## Parallel checking

Stream checking runs in a thread pool. The pool size is configurable. Checking is subject to per-M3U-account concurrent stream limits to avoid exceeding provider caps.

Set `check_all_streams: true` in the profile to check every stream on a channel. Default is to check only the currently active (top) stream.

`stream_limit` caps the maximum number of streams checked per channel per run.

---

## Dead stream tracking

Streams that fail checking are marked dead in the `DeadStreamsTracker`. Dead streams are:

- Excluded from quality scoring
- Optionally removed from the channel automatically

If `allow_revive: true` is set in the profile, dead streams are re-checked on each run and restored to the channel if they pass.

---

## Stream protection (hysteresis)

StreamFlow distinguishes between streams that are **currently in use** and streams that are idle. Currently active streams receive a longer grace period before being replaced — even if a higher-scoring stream is available — to avoid interrupting live playback. Idle streams are replaced aggressively.

Set `grace_period: true` in the profile to enable the grace window for checked streams.

---

## Parallel checker

The `parallel_checker.py` module distributes channel check tasks across a configurable worker pool. Progress is logged periodically during large runs.
