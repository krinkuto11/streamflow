# Stream Checking

## Overview

Stream checking is the third step of the automation pipeline. It uses ffmpeg to analyze each stream, scores based on quality dimensions, and reorders streams so the best one is at the top of the channel in Dispatcharr.

---

## Quality checking

Quality probes accept HTTP, HTTPS, RTMP, RTMPS, RTSP and RTSPS sources, including
RTSP cable tuners. Provider profile URL rewrites use the same protocol rules.
The default identity rewrite (`^(.*)$` to `$1`) preserves the configured source
URL and query parameters. Alternate profiles still require their own changed
route, and provider/profile capacity limits apply to these probes as usual.
This is automatic backend behavior; no additional StreamFlow setting is needed.
RTSP probes allow at least 60 seconds of startup buffer in addition to the
configured timeout, media measurement window and completion headroom. Larger
configured startup buffers are preserved. This minimum is backend-only and also
applies to visual/bitrate retries and metadata fallback; HTTP timing is unchanged.

Each stream is analyzed by spawning a short ffmpeg probe session. Extracted metrics:

- Bitrate (kbps)
- Resolution (width × height)
- FPS
- Codec (H.264, H.265/HEVC, AV1, etc.)
- HDR (detected from ffmpeg stderr: pixel format, color space, primaries, transfer function)
- Blank-screen status (optional; parsed from ffmpeg `blackdetect` output)
- Error presence (dropped frames, decode errors)

---

## Missing bitrate and recheck

When an initial probe proves that a stream is playable but cannot measure its
current bitrate, StreamFlow does not reuse an older bitrate as the current
measurement. After all initial probes for that channel finish, the affected
streams receive a lightweight bitrate-only recheck one at a time, in stable
stream order, before the next channel starts.

The recheck uses the configured probe timing and existing provider, profile,
and global capacity limits. It does not repeat blank/freeze decoding, and a
failed recheck does not turn the playable stream into a dead stream or erase
the initial visual evidence. If the retry succeeds, the recovered value becomes
the current bitrate. If it still cannot be measured, the current result stays
`N/A` with `Bitrate unavailable after recheck`; any older stored bitrate remains
ranking-only evidence.

This is automatic backend behavior, not a user setting. Current activity is
visible at `Stream Checker -> Current Progress (active run) -> Stream Progress
Tracking -> Status -> Bitrate Recheck`; completed evidence is under `Changelog
-> Action filter: Automation Runs -> Automation Period (expand) -> <channel> ->
Quality Check (expand) -> Analyzed Streams -> Reason`.

---

## Scoring

Streams are scored 0–100 using weighted dimensions. Configure the weights at
`Settings -> Profiles tab -> Edit profile -> Stream Checking -> Stream Quality
Scoring`; `scoring_weights` is the corresponding profile configuration object.

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

Before scoring, streams can be discarded based on minimum thresholds. Configure
resolution, FPS, and bitrate at `Settings -> Profiles tab -> Edit profile ->
Stream Checking -> Minimum Quality Requirements`, and blank detection at the
same Stream Checking step under `Check streams for blank screens`.

| Field            | Effect                                                      |
| ---------------- | ----------------------------------------------------------- |
| `min_resolution` | Skip streams below this resolution                          |
| `min_fps`        | Skip streams below this FPS                                 |
| `min_bitrate`    | Skip streams below this bitrate (kbps)                      |
| `blank_check_enabled` | Mark streams dead when most of the probe window is blank |

Blank detection is folded into the same ffmpeg process as the quality probe.
It adds a second ffmpeg output from the already-open input instead of starting
ffprobe or a second provider connection, so single-stream provider limits are
respected.

---

## Parallel checking

Configure concurrent checking at `Stream Checker -> Stream Checker Configuration
-> Edit -> Concurrent Checking tab`.

Stream checking runs in a thread pool. The pool size is configurable. Distinct
usable credential-route components represent independent provider credentials
and are enforced separately. Profiles that resolve to the same credential
target, including default aliases, share one component whose capacity is the
strictest finite limit among those aliases. Finite component limits are summed
for the account aggregate; if any distinct component is unlimited
(`max_streams: 0`), the aggregate is unlimited. The M3U account `max_streams`
value is used only as a fallback when the account has no active provider profile
credentials. If active profiles exist but none can provide a usable route for a
stream, that check fails closed instead of falling back to the stored URL.

Every probe URL is resolved while reserving its exact profile and remains bound to that reservation. A default profile may use the stored provider URL; every non-default profile must produce its own valid credential rewrite or the probe fails closed. The Profile Matrix is read-only status/API information; profile limits and credential rewrites are not editable there.

Provider capacity is never inferred from malformed or missing authority. A
missing account/profile inventory or an invalid route ends that probe immediately
with `provider_profile_unavailable`; an unavailable or malformed live proxy-status
usage read is `provider_usage_unavailable` and waits safely until the configured
timeout. Neither condition is treated as zero usage. Inspect `Stream Checker ->
Current Progress (active run) -> Stream Progress Tracking -> Status/reason` or
`GET /api/stream-checker/progress -> streams_detail[].reason_detail|skipped_reason`
before changing limits.

The matrix is visible during an active run at `Stream Checker -> Current
Progress (active run) -> Profile Matrix (expand)`; its API source is `GET
/api/stream-checker/progress -> provider_progress[].profile_slots[]`.

Per-stream reservation telemetry is visible at `Stream Checker -> Current
Progress (active run) -> Stream Progress Tracking -> Account` (profile name;
hover for ID and Limit), and at `GET /api/stream-checker/progress ->
streams_detail[].reserved_profile_id|reserved_profile_name|reserved_profile_limit`.
The reported limit is the capacity actually enforced for that reservation; when
profile aliases share one credential route, it is their strict shared-route limit
rather than a looser raw profile value. It contains no probe URL or credentials.
Waiting or viewer-preempted rows clear a released profile; an initial probe and
serial bitrate recheck may therefore show different exact profiles. A completed
live row retains the profile that actually performed its final probe.

Long loop-detection probes use the same account and profile reservations, apply the URL transformation of the profile they actually reserve, and stop without recording a clean result when manual cancellation or real-viewer preemption occurs.

Enable `Check All Streams in Channel` at `Settings -> Profiles tab -> Edit
profile -> Stream Checking` to check every stream on a channel. The profile key
is `check_all_streams`; by default only the currently active (top) stream is
checked.

`Stream Limit per Channel` at `Settings -> Profiles tab -> Edit profile -> Stream
Checking` caps the maximum number of streams checked per channel per run; its
profile key is `stream_limit`.

---

## Dead stream tracking

Streams that fail checking are marked dead in the `DeadStreamsTracker`. Dead streams are:

- Excluded from quality scoring
- Optionally removed from the channel automatically

If `allow_revive: true` is set in the profile, dead streams are re-checked on each run and restored to the channel if they pass.

---

## Connectivity guard

Stream checking includes a fail-closed connectivity guard before destructive
quality-check operations. By default, StreamFlow verifies both general internet
reachability and the configured Dispatcharr API before a quality check can mark
streams dead or write a changed stream list back to a channel.

The guard also re-checks connectivity immediately before dead-stream marking and
channel stream updates. If DNS, internet access, the gateway, or the Dispatcharr
API cannot be verified, the quality-check step aborts and leaves channel stream
assignments unchanged.

The guard can be disabled at `Stream Checker -> Stream Checker Configuration ->
Edit -> Safety tab -> Connectivity Guard` or by setting:

```json
{
  "connectivity_guard": {
    "enabled": false
  }
}
```

---

## Stream protection (hysteresis)

StreamFlow distinguishes between streams that are **currently in use** and streams that are idle. Currently active streams receive a longer grace period before being replaced — even if a higher-scoring stream is available — to avoid interrupting live playback. Idle streams are replaced aggressively.

Enable `Respect 2h Grace Period` at `Settings -> Profiles tab -> Edit profile ->
Stream Checking` to enable the grace window for checked streams; its profile key
is `grace_period`.

---

## Concurrent checking

Concurrent stream probes are handled inside the stream checker service with
account-aware limits. Progress is logged periodically during large runs.


## Dispatcharr provider profile permissions

StreamFlow applies the authenticated Dispatcharr user's `custom_properties.allowed_m3u_profile_ids` to provider probes. The value is read from `GET /api/accounts/users/me/` with the configured API key or username/password account. A missing or null value is unrestricted, an empty list allows no provider profiles, and a list of IDs allows only those active profiles. No additional StreamFlow setting is needed. Configure the allowlist in Dispatcharr: **System -> Users -> Edit user -> Permissions -> Allowed Provider Profiles**. Sign in as another administrator to edit the StreamFlow user; Dispatcharr hides the Permissions tab when editing your own account.

The full provider inventory remains available for viewer accounting. Only allowed profiles contribute checker capacity or appear as usable slots in the Stream Checker Profile Matrix. Viewers on another profile that shares the same credential route still consume its capacity; viewers on unrelated forbidden credentials do not consume allowed-profile capacity.

Parallel, sequential, one-off, bitrate recheck, loop and Shadow Monitor probes use the same permissions and reservation rules. Restricted users never fall back to the original provider URL when no allowed profile is usable. Missing or unreadable permissions skip a check with an explicit reason and do not produce a new dead-stream result. Existing dead-stream history is retained; use a deliberate recheck to reevaluate earlier failures.

Rights are cached in memory for at most 15 seconds and refreshed at the start of ordinary probe runs. Failed refreshes revoke check permission and are retried after a short backoff. Connection identity changes invalidate cached rights. A changed permission snapshot cannot commit a reservation resolved against older permissions; active probes use the existing preemption checks to stop when their reservation loses authority.
