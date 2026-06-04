# PR 432 V3 Changelog And Validation

This document is the detailed changelog for PR #432. It keeps the pull request body readable while preserving the V3 branch scope, validation record, current test image, and remaining draft gates.

## Branch Scope

Base: `krinkuto11:dev`

Head: `bttfw:v3/dashboard-manual-quality-stage-state`

PR: https://github.com/krinkuto11/streamflow/pull/432

Current test image: `ghcr.io/bttfw/streamflow:dashboard-manual-quality-stage-state`

Package page: https://github.com/bttfw/streamflow/pkgs/container/streamflow

## Changelog

### Stream Checker And Provider Capacity

- Adds provider/profile-aware slot reservation for stream checks.
- Shows per-account profile slot usage and reserved profile names during Stream Checker progress.
- Adds safe profile-slot ID and limit details to Stream Checker tooltips without exposing credentials.
- Adds an expandable Stream Checker profile matrix with safe profile ID, limit, active viewer, checking, free-slot, and status details.
- Lets real viewers preempt quality probes when the viewer needs the slot.
- Cleans up manual quality queue progress and stage semantics.
- Honors both `m3u_account_id` and legacy `m3u_account` stream payloads for UDI indexes, slot reservation, and stream sorting.
- Clarifies when Playlist Priority Rank is applied and when score-only priority modes ignore it.
- Adds safer queue status counters for active dead, blank, and freeze classifications so dashboard cards do not stay at zero during active batches.
- Improves quality classification reasons for viewer preemption, provider wait timeouts, capacity waits, connectivity errors, stream timeouts, and generic analysis failures.
- Adds ETA states that show `Learning ETA`, then `Early ETA`, before switching to normal remaining-time estimates after enough timing data exists.

### Teamarr Preflight And Events

- Adds managed-event preflight filters and operator UX refinements.
- Shows selected quality profile behavior directly in Teamarr Preflight, including Quality Check, Dead Removal, Blank Detection, Freeze Detection, and Loop Check.
- Splits timing guidance into explicit buckets: main preflight at `-20 min`, pre-start retries at `-10 min` and `-3 min`, and post-start checks at `+2 min` and `+4 min`.
- Clarifies that `Poll Interval` is the Teamarr API polling interval, while `Pre-Start Retries` and `Post-Start Checks` are comma-separated minute offsets rather than continuous monitoring.
- Adds `post_start_offsets_minutes=[2,4]` for new default configs without silently changing already-saved configs.
- Adds shared search, filtered counts, empty search states, and larger managed/recent event windows.
- Keeps up to 1000 managed Teamarr event candidates in backend status and shows the first 100 in the UI with a `Show all` option for large schedules.
- Shows managed-record, candidate, returned, and truncation counts so missing event channels can be distinguished from filtered or unsupported Teamarr payloads.
- Accepts alternate Teamarr start-time and Dispatcharr channel-id field names for managed event channels.
- Shows active event checks with event, channel, bucket, and start-time context.
- Shows active event-check runtime so long-running Teamarr checks are easier to spot.
- Keeps active checks searchable in the Teamarr event list.
- Surfaces post-start event-channel behavior in Help and Operational Notes.
- Queues due Teamarr event checks when direct preflight capacity is already occupied, instead of hiding them until the next poll.
- Documents that event priority sorts waiting checks only; it does not interrupt the channel currently being checked.
- Shows the next automatic check bucket/time on scheduled Teamarr event cards and labels the manual event action as `Force Check`.

### Dashboard, Startup, And Automation Progress

- Adds the `Initializing StreamFlow` startup gate.
- Makes UDI cache readiness and startup status endpoints non-blocking where practical.
- Preserves cache data on empty UDI fetches.
- Adds context-aware dashboard run metrics for queued and single quality checks.
- Aligns Dashboard counters with active Stream Checker batch counters.
- Labels automation assignment changes as `Channels Updated`.
- Exposes accepted M3U refresh request progress through the existing `M3U Refresh` stage.
- Shows planned, requesting, accepted, failed, and skipped refresh states.
- Clarifies startup duration with `Elapsed`, `Expected`, and `Remaining` labels instead of only generic waiting text.

### Shadow Monitor And Viewer Activity

- Improves continuous watcher stability for long-lived viewer probes.
- Labels the rolling per-channel switch guard as `Channel Switch Limit`.
- Keeps switch limit behavior separate from same-channel cooldown.
- Shows sanitized watcher-client continuity for reconnect diagnosis.
- Records watcher reconnects as `Watcher Recovered` events when continuity is restored.
- Keeps real viewer wording distinct from shadow watcher clients in Dashboard and Shadow Monitor UI.

### Session Runtime Stability

- Reinitializes volatile stream-session refresh state on reused singleton instances.
- Prevents session creation from failing when an existing manager instance is missing transient runtime fields after unusual import, test, or reload ordering.

### Hardware Diagnostics And Setup Help

- Adds hardware status display and startup diagnostics.
- Distinguishes NVIDIA, Intel/DRI/VAAPI/QSV, explicit no-NVIDIA, and FFmpeg-method-only runtime states.
- Treats FFmpeg-reported `drm`, `qsv`, and `vaapi` as valid DRI hardware signals.
- Avoids NVIDIA runtime probing and warning when the selected hardware path is DRI-only.
- Keeps CPU fallback behavior visible and documents when checks are CPU-only, hardware preferred with fallback, hardware-only, or at risk.
- Adds platform-neutral Intel/DRI Compose guidance for `/dev/dri`, render-group access, CPU fallback, API host/port alignment, and healthcheck alignment.
- Resolves DRI `auto` stream analysis to a concrete VAAPI render node for the FFmpeg command and logs the requested mode/device/fallback/decode-filter path per probe.
- Leaves CUDA/NVIDIA mode and explicit VAAPI/QSV selections unchanged while making DRI auto behavior observable.

### In-App Help And Operator Guidance

- Adds a Sidebar Help route for startup/cache, accepted playlist refresh requests, profiles/periods, Stream Checker, Shadow Monitor, hardware/fallback, and troubleshooting.
- Adds detailed `Where` locations for Settings and Controls cards.
- Verifies Help setting locations against real visible UI paths.
- Labels non-editable status, API, or backend-only values instead of presenting them as user-editable settings.
- Adds a `Quality reason details` Status/API card with elapsed/limit, probe window, startup buffer, attempts, host, HTTP status, timeout seconds, and wrapped API-field paths.
- Uses explicit operator wording for checker-capacity and global-worker quality reasons instead of raw fallback codes.
- Ships Help screenshots as explicit collapsible/lazy UI references with fixed dimensions, tight crops, optimized size, and no raw full-screen proof captures.
- Adds setup guidance for post-start event-channel checks, Shadow Monitor switch guard behavior, and post-setup smoke checks.

### Automation Policies

- Adds GUI-editable Startup catch-up policy for safe first/no-last-run cases.
- Adds GUI-editable Missed-run grace for automatic missed scheduled runs.
- Tracks bounded missed-run skip history and latest skip reason.
- Adds explicit `Run all due periods` opt-in so automatic scheduler passes do not silently process every due period by default.
- Adds global automatic-run policies for Catch-up cap, Maintenance window, and Teamarr event window.
- Keeps manual forced runs separate from automatic-run policy pauses.

## Validation

### Local

- Focused backend and frontend tests have been run for each V3 slice before push.
- Frontend production builds have passed after UI/help changes.
- `git diff --check` has been kept clean apart from normal CRLF notices in the Windows workspace.
- The functional Help follow-up head passed `npm.cmd run test:ci`, `npm.cmd run build`, and a local dark-mode `/help/troubleshooting` Playwright render before push.
- The latest DRI/Teamarr/catch-up/session head passed focused backend tests, full frontend Vitest, frontend production build, and the full backend suite with 1094 passed, 2 skipped.
- Feature-change head `4b151df` passed `python -m pytest tests/test_teamarr_preflight_service.py -q`, `npm.cmd test -- teamarr-preflight-schedule.test.js --run`, `npm.cmd run build`, `python scripts/run_ci_checks.py`, and full `npm.cmd run test:ci` with 117 frontend tests.

### GitHub

- PR checks passed on the feature-change head and were rerun after documentation
  refreshes:
  - Backend smoke tests
  - Frontend build and tests
  - CodeQL Python analysis
  - CodeQL JavaScript analysis

### Image And Live

- Test image tag: `ghcr.io/bttfw/streamflow:dashboard-manual-quality-stage-state`
- The PR body tracks the mutable current image build URL, manifest digest, and deployed commit for external testing.
- Current V3 test images have been live-loaded and smoke-tested against API health, initialization, hardware status, Stream Checker status, Teamarr Preflight status, Shadow Monitor status, Auto-Create-Rules, browser DOM checks, and post-deploy log scans.
- Recent live smoke completed with 217513 streams, 220 channels, and 6 accounts loaded.
- Live Teamarr Preflight smoke on `4b151df` confirmed `Vegas Golden Knights at Carolina Hurricanes` shows the next automatic check `-20m` bucket and an enabled `Force Check` button in dark mode.
- Read-only live smoke on `4b151df` confirmed `/scheduling`, the Auto-Create dialog opening path, and `/teamarr-preflight` without interface text errors or console errors.
- A 10-minute idle observation on a functionally identical code head passed with 20 API samples, `bad_count=0`, no Stream Checker/Teamarr/Shadow last errors, an empty 12-minute log scan, and the service still healthy.
- Active-viewer Shadow Monitor observation passed on the live deployment: 12 samples, at least one real viewer client, at least one watcher client, one watched channel, `watching` state, max watcher uptime 135 seconds, `bad_count=0`, and no Shadow last error.

## Release Gate

This PR must remain draft until:

- Any remaining V3 polish requested by the operator is finished.
- The latest image has completed multi-arch build and live smoke validation.
- Help pages include the required platform-neutral setting explanations and verified locations.
- Shipped Help screenshots are cropped, optimized, collapsible/lazy, and useful enough to justify image size.
- Final release notes are reviewed and explicitly approved by the operator.
