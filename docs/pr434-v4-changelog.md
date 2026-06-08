# PR 434 V4 Changelog And Validation

This document is the detailed changelog for PR #434. It keeps the pull request
body readable while preserving the V4 release-hardening scope, validation
record, current image, screenshot evidence, and remaining draft gates.

## Branch Scope

Base: `krinkuto11:dev`

Head: `bttfw:v4/release-hardening`

PR: https://github.com/krinkuto11/streamflow/pull/434

Current image tag: `ghcr.io/bttfw/streamflow:release-hardening`

Package page: https://github.com/bttfw/streamflow/pkgs/container/streamflow

The PR body tracks the mutable current PR head, image build URL, manifest
digest, deployed commit, screenshots, and remaining release gates.

## Changelog

### Auto-Create And Scheduled Events

- Repairs Auto-Create as a generic EPG-regex scheduler for selected channels
  and channel groups.
- Uses live channel identity and TVG IDs during rule matching instead of stale
  embedded channel snapshots.
- Fetches paginated EPG programs so large schedules are not silently truncated.
- Avoids the immediate double-match path on create/update requests and keeps
  expensive EPG matching in the background.
- Enqueues Auto-Create event checks through the Stream Checker queue at
  priority `90`.
- Reports total matches separately from schedulable future events, due-now
  queued checks, already-checked events, missing time, and invalid time so a
  popup count like `24 matched` can be reconciled with fewer visible Scheduled
  Events.
- Keeps past, already-due, and already-checked event outcomes visible in the
  refresh summary instead of making them look like lost team channels.

### Queue, Preflight, And Event Checks

- Serializes direct Teamarr preflight, queued event checks, Auto-Create checks,
  and synchronous full-run quality batches through the same stream-checking
  path.
- Keeps queued Teamarr and Auto-Create checks visible while another check is
  active.
- Preserves queue status counters for queued, in-progress, completed, failed,
  good, blank, frozen, dead, and ETA state during active batches.
- Keeps Teamarr Preflight disabled by default in the live release test while
  still validating queue isolation with forced/run-once slices.

### M3U Refresh And Automation Progress

- Waits for accepted Dispatcharr playlist refreshes to settle before cache sync.
- Shows `waiting`, `settled`, `partial`, `timeout`, and `failed` refresh states.
- Observes already-running playlist parsing instead of racing cache sync against
  Dispatcharr.
- Retries failed providers only when possible and continues with healthy
  providers for partial refresh outcomes.
- Adds cache-sync, stream-matching, quality-queueing, and quality-checking
  progress messages that survive long full runs.
- Extends the automation busy guard for production-scale full runs so a real
  multi-hour quality batch is not mistaken for a stale lock after one hour.

### Stream Checker, ETA, Blank, And Freeze

- Uses conservative full-run ETA floors until enough real channel timing is
  known.
- Labels provider-limited or floor-based estimates as `Rough ETA` with a
  tooltip because long provider waits can swing between channels.
- Keeps synchronous batch dead, blank, and frozen result counts visible while a
  run is active and after finalization.
- Adds `Good Streams` and `Checking now` to the dashboard Automation Run card
  without adding another large panel.
- Shows low-quality reasons directly under the `Low Quality` status badge in
  Stream Checker rows.
- Preserves checked stream detail in quality summaries so the changelog and API
  reports describe what was actually analyzed.
- Keeps connectivity guard status visible during automation-triggered stream
  checks.

### Shadow Monitor And Viewer Context

- Gates Shadow Monitor startup on watcher API-key readiness.
- Keeps active viewer cards tied to visible EPG context.
- Sanitizes Shadow Monitor error/status responses so raw backend exception
  detail is not shown to operators.
- Keeps Shadow Monitor running during full-run smokes without requiring
  Teamarr Preflight to be enabled.

### UI And Release Polish

- Removes the unused AceStream Monitoring surface while keeping Stream
  Monitoring, Stream Checker, Shadow Monitor, and Teamarr Preflight intact.
- Clarifies Teamarr event-window setting labels and help copy.
- Fixes narrow/mobile Scheduling toolbar wrapping in dark mode.
- Keeps PR #434 in Draft/do-not-merge state until explicit release approval.

## Validation

### Local

- Final Teamarr Preflight UI null-loading follow-up passed on code-validation
  head
  `d94d244417f958aad7c862abb0954d64af5e9c5d`:
  - `npm.cmd --prefix frontend run test:ci -- src/lib/teamarr-preflight-status-display.test.js src/lib/teamarr-preflight-event-search.test.js src/lib/teamarr-preflight-schedule.test.js src/lib/teamarr-preflight-event-health.test.js src/lib/teamarr-preflight-filters.test.js`
    -> `5` files, `24` tests passed.
  - `npm.cmd --prefix frontend run test:ci`
    -> `19` files, `143` tests passed.
  - `npm.cmd --prefix frontend run build`
    -> passed with the existing Browserslist and Vite chunk warnings.
- Post-full-run Shadow/Teamarr service follow-up passed on branch head
  `a55dfe2e91c9037faf50f3a7614a64af808df137`:
  - `python -m pytest -q backend/tests/test_shadow_blank_monitor_service.py`
    -> `38 passed`.
  - `python -m pytest -q backend/tests/test_teamarr_preflight_service.py`
    -> `41 passed`.
  - Combined targeted gate -> `79 passed`.
- Auto-Create backend/API targeted gate passed on branch head `2970cc4`:
  `26 passed`.
- Auto-Create frontend display gate passed on branch head `2970cc4`:
  `9 passed`.
- Queue, Scheduling worker, Stream Checker lifecycle, regex matching, and
  Teamarr Preflight targeted gate passed on branch head `2970cc4`: `83 passed`.
- Frontend production build passed on branch head `2970cc4` with the existing
  Vite/Browserslist/chunk warnings.
- Mobile Scheduling toolbar follow-up passed frontend targeted tests and
  production build on branch head `81259ad`.
- Automation busy guard follow-up passed
  `backend/tests/test_udi_automation_busy_guard.py`,
  `backend/tests/test_scheduling_workers.py`, and
  `backend/tests/test_stream_checker_queue_lifecycle.py` on branch head
  `40c1d18`: `42 passed`.
- Post-full-run UI/metrics follow-up passed:
  - `python -m py_compile backend/apps/stream/stream_checker_service.py backend/apps/automation/automated_stream_manager.py`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q backend/tests/test_stream_checker_queue_lifecycle.py backend/tests/test_automation_run_progress.py -x`
    -> `45 passed`
  - `npm.cmd run test:ci -- dashboard-run-counts.test.js queue-eta-display.test.js quality-reason-display.test.js`
    -> `27 passed`
  - `npm.cmd run build` -> passed with the existing Vite/Browserslist/chunk
    warnings.
- `backend/apps/automation/scheduling_service.py`,
  `backend/apps/api/schemas.py`, `backend/apps/api/scheduling_handlers.py`,
  and `backend/apps/udi/manager.py` compiled successfully with `py_compile`.

### GitHub

- PR checks passed on final code-validation head
  `d94d244417f958aad7c862abb0954d64af5e9c5d`: Backend smoke tests,
  Frontend build/tests, CodeQL JavaScript, CodeQL Python, and CodeQL summary
  were all `SUCCESS`.
- GHCR image build passed on the fork for final code-validation head
  `d94d244`:
  https://github.com/bttfw/streamflow/actions/runs/27124960707
- Final code-validation `ghcr.io/bttfw/streamflow:release-hardening`
  manifest digest:
  `sha256:6371c04ceece8393d4e0d43bc57f0b69188db6e98152f3b6f9873477cc571fec`.
- PR checks passed on head `40c1d18e18732052c8865ea7d802ccf8cdc2dca2`:
  - Tests: https://github.com/krinkuto11/streamflow/actions/runs/27051354454
  - CodeQL: https://github.com/krinkuto11/streamflow/actions/runs/27051354445
- GHCR image build passed on the fork for the same runtime head:
  https://github.com/bttfw/streamflow/actions/runs/27051357148

### Image And Live

- Final code-validation runtime head:
  `d94d244417f958aad7c862abb0954d64af5e9c5d`.
- Final code-validation digest:
  `ghcr.io/bttfw/streamflow@sha256:6371c04ceece8393d4e0d43bc57f0b69188db6e98152f3b6f9873477cc571fec`.
- The final live runtime was built by GitHub Actions/GHCR and deployed through
  the normal Unraid DockerMan update path with repository
  `ghcr.io/bttfw/streamflow:release-hardening`.
- Live image smoke passed after deploy: StreamFlow health `healthy`,
  RestartCount `0`, version `release-hardening-20260608`, automation idle,
  Stream Checker idle, Teamarr Preflight idle/connected, Shadow Monitor
  enabled/running, and viewer/watcher counts `0`.
- Dispatcharr was updated through the normal Unraid update path to the live
  `latest` image with version label `0.26.0`. StreamFlow then completed UDI
  startup sync against the updated Dispatcharr data: `217810/217810` streams,
  `214/214` channels, `214/214` logos, and `6` playlists.
- The final live API matrix on `d94d244` passed across `30/30` read-only
  endpoints: automation idle, UDI ready, Stream Checker queue empty,
  Teamarr queue empty, Shadow last_error `null`, no viewers/watchers,
  Dispatcharr initialization `completed` at `100%`, and scheduling worker
  threads alive.
- The final live UI matrix on `d94d244` passed for Dashboard, Stream Checker,
  Teamarr Preflight, Shadow Monitor, Settings/Profiles, Settings/Scheduling,
  Changelog, Stream Monitoring, and mobile Teamarr Preflight. No Interface
  Error, page error, or blocked loading state was observed.
- The final Teamarr queue-active UI probe forced a controlled event check while
  a Stream Checker item was already active. The UI showed queued and
  queue-active states, the queue drained cleanly, and final counters were
  `queued=0`, `queue_active=0`, `completed=2`, `failed=0`.
- The final watcher/restart safety check kept the Unraid User Scripts disabled:
  `Restart Tv` and `Streamflow Check` both had `frequency=disabled`, no root
  crontab entries existed for them, no watcher/user-script process was
  running, and the 2026-06-08 00:00-08:00 log scan found no Telegram/Unraid
  notification or restart trigger evidence.
- The d94d244 follow-up did not change Auto-Create, BossSports, or Team
  channel matching behavior. The Scheduling route itself was not opened during
  the final UI smoke because it auto-loads Auto-Create rule data on page load;
  Scheduling/EPG was verified read-only through status/config APIs and the
  Settings -> Scheduling UI tab instead.
- Live Auto-Create group preview over NBA/MLB test groups found `14` EPG
  matches across `14` channels with `0` missing TVG IDs.
- Live Auto-Create scheduled outcome proof on the release image reported
  `14` total EPG matches, `2` schedulable matches, `1` future match,
  `1` due-now match, `12` already-checked matches, and `0` missing/invalid
  time matches.
- Live Auto-Create queue proof completed the due event checks with `0` failed
  and restored the temporary test channels to their previous EPG profile state.
- Live Teamarr Preflight run-once smoke found `14` candidates, launched one
  direct check, and returned to `0` active / `0` queued checks.
- Live Blank/Freeze/Shadow smoke confirmed Shadow Monitor enabled/running with
  no last error and no stuck Queue/Preflight work.
- Final full-run observation on runtime head `40c1d18` completed as run
  `automation-1780716966-1`: `212/212` channels, `0` failed,
  `0` incomplete, `3585` streams analyzed, `789` dead, `308` blank,
  `395` frozen, `124` revived, `45` added, M3U refresh `settled`,
  cache sync `2/2`, Shadow Monitor still running, no Preflight queue residue.
  Duration was `26438.287` seconds. Evidence:
  `outputs/streamflow-v4-final-full-run-live/final-full-run-completion-summary-20260606-125929.json`.
- Final log scan for the completed full run reported `NO_MATCHES` for fatal
  tracebacks/errors in
  `outputs/streamflow-v4-final-full-run-live/final-precise-log-scan-20260606-125951.txt`.
- Preflight-enabled queue slice after the full run completed with one manual
  Stream Checker batch entry plus one forced Teamarr event check, saw
  `preflight_queued` and `preflight_completed`, ended with queue
  `completed=2`, `failed=0`, and restored `preflight_enabled=false`.
  Evidence:
  `outputs/streamflow-v4-preflight-queue-slice/preflight-enabled-queue-slice-summary-20260606-130459.json`.

### Screenshots

- Final `d94d244` live screenshots were captured for Dashboard, Stream Checker,
  Teamarr Preflight, Shadow Monitor, Settings/Scheduling, Changelog, Stream
  Monitoring, and mobile Teamarr Preflight. Public-safe main-content crops were
  also captured without the environment sidebar. They are retained in the
  external live-evidence bundle rather than embedded in source because the live
  instance contains environment-specific URLs and operator data.
- Dark-mode screenshots were captured for Scheduling, Stream Checker, Teamarr
  Preflight, Shadow Monitor, and Changelog.
- Raw desktop proof captures are kept out of the app/PR because they include
  environment sidebar details; final PR evidence should use cropped/sanitized
  main-content screenshots only.
- Mobile Scheduling wrapping was rechecked after the responsive fix with
  `docScrollWidth=390` and no page-level horizontal overflow outside the
  intentionally off-canvas mobile sidebar.
- Dark-mode UI smoke for the post-full-run polish showed `Good Streams`,
  `Checking now`, `Rough ETA`, and low-quality reason text without horizontal
  overflow at `1920px`. Evidence:
  `outputs/streamflow-v4-ui-smoke/dashboard-good-checking-now-local-20260606-1325.png`
  and
  `outputs/streamflow-v4-ui-smoke/streamchecker-low-quality-eta-local-20260606-1325.png`.

## Release Gate

Release gate status for code-validation head `d94d244`: complete.

- Final code fixes were pushed and CI/GHCR passed.
- Final image was deployed only through GitHub Actions/GHCR plus the normal
  Unraid DockerMan update path; no local Docker build was used.
- Final live matrix and watcher/restart safety checks are green.
- Auto-Create, BossSports, and Team channel matching were not changed by the
  final hardening follow-up.
- PR body and this detailed changelog have been refreshed with final head,
  digest, checks, live-smoke, screenshots/evidence, and full-run outcome.
- Documentation-only refresh commits after `d94d244` do not change runtime
  behavior, but they still need the normal CI/GHCR/deploy confirmation before
  the PR is marked ready.
- PR #434 may be marked ready/mergeable after the final documentation refresh
  is also built, deployed, and reflected in the PR body. The Unraid watcher
  schedule remains disabled until the post-gate operator path intentionally
  re-enables it.
