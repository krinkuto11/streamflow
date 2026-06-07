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

- PR checks passed on head `40c1d18e18732052c8865ea7d802ccf8cdc2dca2`:
  - Tests: https://github.com/krinkuto11/streamflow/actions/runs/27051354454
  - CodeQL: https://github.com/krinkuto11/streamflow/actions/runs/27051354445
- GHCR image build passed on the fork for the same runtime head:
  https://github.com/bttfw/streamflow/actions/runs/27051357148

### Image And Live

- Current live runtime head:
  `40c1d18e18732052c8865ea7d802ccf8cdc2dca2`
- Current live digest:
  `ghcr.io/bttfw/streamflow@sha256:bf353a09c8ac6abf4c361a3f6d8b252edd970d78cf4364563c522edfea8d5c09`
- The live runtime was updated through the standard release deployment
  workflow with repository `ghcr.io/bttfw/streamflow:release-hardening`.
- Live image smoke passed after deploy: health healthy, version
  `release-hardening-20260606`, automation idle, Stream Checker idle, Teamarr
  Preflight idle, Shadow Monitor enabled/running, Auto-Create rules/events
  clean.
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

This PR must remain draft until:

- The post-full-run UI/metrics follow-up is pushed, CI/GHCR succeeds, and the
  resulting image is deployed through the standard release deployment workflow.
- Final live image smoke confirms health/version, Stream Checker/Preflight
  idle state, dashboard run metrics, low-quality reason display, and dark-mode
  screenshots on the final image.
- PR body is updated with final head, digest, checks, live-smoke, screenshots,
  and full-run outcome.
- Final sanitized screenshots are committed or explicitly waived.
- Final release approval is given for ready/merge state.
