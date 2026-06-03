# V3 Draft Release Notes

These notes summarize the V3 draft branch for operator review before release.

## Status

V3 remains a draft until the operator explicitly approves release. The branch has
been locally tested, CI-tested, image-built, and live-smoked through the existing
template update path.

## Highlights

- Stream Checker now reserves provider/profile capacity more explicitly and keeps
  real viewer activity protected from quality probes.
- Dashboard and Stream Checker progress wording now separates learning ETA,
  early ETA, no-due idle state, refresh requests, cache sync, stream matching,
  queueing, and quality-check stages.
- Automation runs preserve manual-stop semantics, expose clearer run metrics, and
  keep skipped M3U refresh/cache sync stages visibly neutral.
- Teamarr-managed event checks support queued event preflights, manual checks for
  past events, event/channel/date matching, post-start checks after game start,
  and focused operator controls.
- Automation periods now include startup catch-up, missed-run grace,
  missed-run skip history, a global catch-up cap, and a maintenance window for
  automatic runs.
- Scheduling auto-create rule previews now test every selected channel and every
  channel in selected groups instead of sampling one channel from the selection.
- Hardware acceleration stays optional and visible, with CPU-only,
  hardware-preferred fallback, and hardware-only states separated. Intel/DRI
  paths can report VAAPI, QSV, or DRI methods without requiring NVIDIA runtime
  checks.
- In-app Help covers startup/cache, profiles/periods, Stream Checker, Shadow
  Monitor, hardware/fallback, and troubleshooting.

## Operator Changes

- `Post-Start Checks` in Teamarr Preflight lets event checks run after game
  start when event channels appear late.
- `Pre-Start Retries` no longer makes a shorter preflight offset trigger early.
- `Missed-run grace` skips stale automatic runs after the configured window and
  records the latest skip reason in Automation Periods.
- `Catch-up cap` limits how many due periods an automatic scheduler pass handles
  at once. Extra due periods are deferred to the next pass.
- `Maintenance window` pauses automatic runs inside a daily time range. Manual
  forced runs still work.
- Hardware status now distinguishes runtime device, FFmpeg methods, DRI methods,
  NVIDIA checks, and CPU fallback state.
- Auto-create rule test results for channel groups now show matches from all
  resolved channels and call out partial TVG-ID coverage.

## Validation Snapshot

- Backend focused tests, frontend Vitest, production build, and the repository CI
  helper passed on the final V3 draft head.
- GitHub Tests and CodeQL passed on the final V3 draft head.
- The branch image was rebuilt for AMD64 and ARM64, then live-loaded through the
  existing template update path.
- Live API smoke passed for health, version, initialization status, automation,
  Teamarr Preflight, Stream Checker, and hardware status.
- A five-minute live observation stayed healthy with initialization complete,
  Teamarr running, and Stream Checker idle.
- Post-deploy log scans found no app tracebacks, critical errors, internal server
  errors, or unhandled exceptions.

## Release Gate

Do not merge until the operator explicitly approves the draft PR for release.
