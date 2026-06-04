# V3 Draft Release Notes

These notes summarize the V3 draft branch for operator review before release.

## Status

V3 remains a draft until the operator explicitly approves release. The branch has
been locally tested, CI-tested, image-built, and live-smoked through the existing
template update path.

## Highlights

- Stream Checker now reserves provider/profile capacity more explicitly and keeps
  real viewer activity protected from quality probes. Profile-slot tooltips now
  include safe ID and limit details for debugging without exposing credentials.
- Dashboard and Stream Checker progress wording now separates learning ETA,
  early ETA, no-due idle state, refresh requests, cache sync, stream matching,
  queueing, and quality-check stages.
- Stream session creation now recovers missing volatile refresh state on reused
  manager instances instead of failing during unusual import or reload ordering.
- Automation runs preserve manual-stop semantics, expose clearer run metrics, and
  keep skipped M3U refresh/cache sync stages visibly neutral.
- Teamarr-managed event checks support queued event preflights, manual checks for
  past events, event/channel/date matching, post-start checks after game start,
  provider-limit override for event windows, active-check runtime visibility,
  and focused operator controls.
- Teamarr Preflight keeps large managed-event schedules visible, shows managed
  record/candidate counts, accepts alternate start-time and channel-id fields
  from Teamarr, and queues due event checks when direct preflight capacity is
  already occupied.
- Scheduled Teamarr event cards show the next automatic check bucket/time and
  label the manual event action as `Force Check`, making planned events with no
  previous check easier to read.
- Automation periods now include startup catch-up, missed-run grace,
  missed-run skip history, explicit run-all-due opt-in, a global catch-up cap,
  and a maintenance window for automatic runs.
- Scheduling auto-create rule previews now test every selected channel and every
  channel in selected groups instead of sampling one channel from the selection.
- Hardware acceleration stays optional and visible, with CPU-only,
  hardware-preferred fallback, and hardware-only states separated. Intel/DRI
  paths can report VAAPI, QSV, or DRI methods without requiring NVIDIA runtime
  checks.
- DRI `auto` stream analysis resolves to a concrete VAAPI render node for the
  FFmpeg command and logs the requested hardware mode/device per probe while
  leaving CUDA/NVIDIA and explicit VAAPI/QSV selections unchanged.
- In-app Help covers startup/cache, profiles/periods, Stream Checker, Shadow
  Monitor, hardware/fallback, and troubleshooting.

## Operator Changes

- `Post-Start Checks` in Teamarr Preflight lets event checks run after game
  start when event channels appear late.
- Teamarr scheduled event cards now show `Next auto check` and expose the
  manual one-time action as `Force Check`.
- `Pre-Start Retries` no longer makes a shorter preflight offset trigger early.
- `Missed-run grace` skips stale automatic runs after the configured window and
  records the latest skip reason in Automation Periods.
- `Run all due periods` is off by default; automatic scheduler passes process
  only the highest-priority due period unless the operator enables it.
- `Catch-up cap` limits how many due periods an automatic scheduler pass handles
  at once when run-all-due is enabled. Extra due periods are deferred to the
  next pass.
- `Maintenance window` pauses automatic runs inside a daily time range. Manual
  forced runs still work.
- Hardware status now distinguishes runtime device, FFmpeg methods, DRI methods,
  NVIDIA checks, and CPU fallback state.
- Auto-create rule test results for channel groups now show matches from all
  resolved channels and call out partial TVG-ID coverage.
- Shadow Monitor defaults now match the validated live recovery path: continuous
  watch mode, live switching when enabled, freeze detection on, 60-second probe
  duration, two confirmations, and per-channel switch-rate protection.

## Validation Snapshot

- Backend focused tests, frontend Vitest, production build, and the repository CI
  helper passed on feature-change head `4b151df`.
- GitHub Tests and CodeQL passed on the feature-change head and were rerun after
  documentation refreshes.
- The branch image was rebuilt for AMD64 and ARM64, then live-loaded through the
  existing template update path.
- Live API smoke passed for health, version, initialization status, automation,
  Teamarr Preflight, Stream Checker, and hardware status.
- Recent live smoke completed startup with 217513 streams, 220 channels, and 6
  accounts loaded.
- Live Teamarr Preflight smoke confirmed the Vegas scheduled event shows
  `Next auto check: 5.6.2026, 01:40:00 (-20m)` and an enabled `Force Check`
  button in dark mode.
- Read-only live Scheduling smoke confirmed navigation, the Auto-Create dialog
  opening path, Teamarr Preflight/Vegas, and no interface or console errors.
- A 10-minute idle live observation on a functionally identical code head stayed
  healthy for 20 API samples with
  initialization complete, Teamarr running, Stream Checker running, Shadow
  Monitor running, `bad_count=0`, and no active watched channel.
- Active-viewer Shadow Monitor observation passed on the live deployment with
  12 samples, at least one real viewer client, at least one watcher client, one
  watched channel, `watching` state, max watcher uptime 135 seconds,
  `bad_count=0`, and no Shadow last error.
- Live `Das Erste HD` proxy validation reproduced black viewer output on the
  channel proxy path while a forced single-channel check re-analyzed 18 streams
  with blank/freeze probes and reported no raw-stream blank/freeze detections;
  Shadow recovery therefore protects the viewer-proxy path that normal stream
  checks cannot reliably represent.
- Post-deploy log scans found no app tracebacks, critical errors, internal server
  errors, or unhandled exceptions.

## Release Gate

Do not merge until the operator explicitly approves the draft PR for release.
