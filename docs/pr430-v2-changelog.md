# PR 430 V2 Changelog And Validation

This document is the detailed changelog for PR #430. It records the V2 branch scope, screenshot evidence, local validation, self-hosted runtime validation, and the remaining final clean-install gate before the PR should move out of draft.

## Branch Scope

Base: `upstream/dev` after PR #429 was merged.

Head: `bttfw:v2/teamarr-preflight-no-api-key-ui`

PR: https://github.com/krinkuto11/streamflow/pull/430

This branch also contains the dashboard automation-stage fix from PR #429 so V2 can be tested as one combined stack.

## Changelog

### Teamarr Preflight

- Adds Teamarr managed-event preflight backend endpoints and service.
- Adds a Teamarr Preflight UI page and sidebar entry.
- Supports include and exclude filters for sports/leagues.
- Supports preflight offsets, retry offsets, cooldowns, and a max concurrent check limit.
- Adds a default Teamarr Event Preflight profile and lets the user choose an existing profile.
- Hides API key values in the UI and API responses.
- Handles guard skips as deferred/retryable work rather than failed checks.
- Clears active preflight slots before terminal/deferred events are recorded so immediate retries do not hit stale concurrency limits.

### Automation And Dashboard Observability

- Normalizes automation stage keys so frontend and backend agree on:
  - `settings`
  - `period_discovery`
  - `m3u_refresh`
  - `cache_sync`
  - `stream_matching`
  - `quality_queueing`
  - `quality_checking`
  - `finalizing`
- Keeps completed stages green instead of greyed out when an automation run is forced from the dashboard.
- Shows live and completed duration boxes instead of `N/A` for M3U refresh, cache sync, stream matching, and quality checking.
- Uses live timing for active dashboard stages.
- Polls live automation and stream-checker status every second while avoiding overlapping status requests, so dashboard cards update without a browser reload.
- Formats sub-second API p95/p99 values as milliseconds instead of rounding them to `0s`.
- Finalizes controlled automation aborts as `aborted` instead of `failed`, and shows the aborted state explicitly in the dashboard.
- Shows freeze counts in dashboard run summaries.
- Shows manual stream-checker progress over skipped automation status.
- Clears stale stream-checker progress when the checker is idle.
- Adds a dashboard route alias fix.
- Adds dashboard viewer activity status.

### Stream Checker And Quality Runs

- Adds queue lifecycle hardening so queue clear and abort states do not leak into the next run.
- Adds better progress observability for stream-checker runs.
- Adds manual run start selection and saved start-order handling.
- Adds provider-limit-aware stream scheduling.
- Adds provider wait/limit observability in the UI.
- Adds controlled capacity waiting across M3U account profiles.
- Scopes single-channel dead-stream cleanup so unrelated channel state is not removed.
- Treats explicit `0 kbps` as a real zero bitrate, preserving dead-stream classification.
- Treats guarded single-channel skips as successful where appropriate.
- Adds blank probe status details in the changelog.
- Fixes the Dispatcharr stream switch endpoint integration.

### Shadow Monitor And Viewer Activity

- Adds active-viewer shadow blank monitor support.
- Adds freeze detection support to the shadow monitor.
- Adds configuration for dry run, cooldowns, confirmation count, watcher API key, concurrent watchers, skip-during-quality-check, and watch mode.
- Keeps the shadow monitor active by default during quality checks; it shadows only real active viewers and does not consume provider stream slots.
- Protects UUID-keyed active viewer channels.
- Adds viewer and watcher status cards to the dashboard.

### Dispatcharr Integration

- Adds Dispatcharr API-key authentication support.
- Waits for startup UDI readiness before automation work depends on channel, stream, and M3U account cache data.
- Adds cache sync stage visibility after playlist refresh.
- Preserves UDI refresh timing and API timing in dashboard status.

### Hardware Acceleration

- Adds optional ffmpeg hardware acceleration settings.
- Supports CUDA mode with CPU fallback.
- Logs hardware acceleration readiness at startup.
- Shows hardware acceleration runtime status.
- Reports ffmpeg availability even when hardware acceleration is disabled, so CPU-only deployments do not look like ffmpeg is missing.
- Covers NVIDIA/GPU runtime visibility in a self-hosted validation stack; validation observed CUDA mode with CPU fallback enabled and the NVIDIA runtime/GPU visible.
- Sanitizes the hardware-status API response so host-specific diagnostics, GPU model names, and raw exception details stay out of client-visible JSON.
- Adds regression coverage in `backend/tests/test_ffmpeg_hardware_acceleration.py`.

### Changelog, Tests, And Docs

- Adds detailed operations guide: `docs/operations-guide.md`.
- Documents normal Docker Compose templates for CPU-only startup validation and NVIDIA/CUDA runtime passthrough.
- Adds dashboard run-count and run-display helpers with frontend tests.
- Updates audited frontend dependencies so `npm audit --audit-level=moderate` reports zero vulnerabilities.
- Adds backend tests for Teamarr preflight, stream checker queue lifecycle, progress observability, hardware acceleration, viewer activity, Dispatcharr stream switching, dead-stream handling, and connectivity guard behavior.
- Cleans up mocks and edge cases in single-channel profile flag tests.
- Restores the broad legacy backend pytest suite by fixing test isolation, stale global limiter state, unsafe import-time DB mutation, and Dispatcharr auth environment leakage.
- Sanitizes unexpected single-channel check API failures so internal exception details are not exposed in client-visible JSON.
- Keeps the PR changelog comment in sync with this file for the final V2 validation record.

## Screenshot Evidence

Current V2 screenshots included in this branch:

- `docs/pr-screenshots/v2-dashboard-stage-status-selfhosted-dark-sanitized.png`
  - Dark-mode self-hosted validation evidence for the dev feedback from PR #428.
  - Shows all automation stages completed and duration boxes populated.
- `docs/pr-screenshots/teamarr-preflight-profile-selector.png`
  - Teamarr Preflight profile selector UI.
- `docs/pr-screenshots/provider-limit-observability.png`
  - Provider limit and stream progress observability.
- `docs/pr-screenshots/hardware-acceleration-settings.png`
  - Hardware acceleration settings UI.
- `docs/pr-screenshots/dashboard-viewer-activity.png`
  - Dashboard viewer activity card.
- `docs/pr-screenshots/v2-gpu-full-run-live-dark.png`
  - Dark-mode evidence from the clean-install GPU passthrough Full Check while Quality Check was actively running.
  - Shows completed stages, populated duration boxes, stream-checker progress, and active Shadow Monitor status.
- `docs/pr-screenshots/v2-dashboard-live-latency-dark.png`
  - Dark-mode evidence from the current V2 branch image.
  - Shows the dashboard live status card and non-zero API p95/p99 latency formatting.
- `docs/pr-screenshots/v2-shadow-monitor-no-quality-guard-dark.png`
  - Dark-mode evidence from the current V2 branch image.
  - Shows the Shadow Monitor configuration after removing the unnecessary quality-check guard control while keeping freeze detection configurable.

Historical screenshots retained from the V2 stack and related precursor PRs:

- `docs/pr-screenshots/pr416-dashboard-shadow-card-live-redacted.png`
- `docs/pr-screenshots/pr416-shadow-monitor-continuous-live-redacted.png`
- `docs/pr-screenshots/pr416-shadow-monitor-live-redacted.png`
- `docs/pr-screenshots/pr8-settings-api-key-auth-sanitized.png`
- `docs/pr-screenshots/pr414-dashboard-quality-progress-live-sanitized.png`
- `docs/pr-screenshots/pr419-shadow-monitor-freeze-live-sanitized.png`
- `docs/pr-screenshots/pr420-run-start-save-start-live-sanitized.png`
- `docs/pr-screenshots/pr420-run-start-tabs-live-sanitized.png`
- `docs/pr-screenshots/pr421-dashboard-duration-eta-live-sanitized.png`
- `docs/pr-screenshots/pr422-dashboard-quality-progress-live-sanitized.png`
- `docs/pr-screenshots/pr423-dashboard-quality-live-sanitized.png`
- `docs/pr-screenshots/pr423-dashboard-route-alias-live-sanitized.png`
- `docs/pr-screenshots/pr427-dashboard-stream-checker-details-sanitized.png`
- `docs/pr-screenshots/pr428-dashboard-cache-counts-live-dark.png`
- `docs/pr-screenshots/pr428-dashboard-manual-check-full-run.png`
- `docs/pr-screenshots/pr428-dashboard-manual-check-live-dark.png`

## Local Validation

Targeted regression set:

```bash
python -m pytest backend/tests/test_teamarr_preflight_service.py::TeamarrPreflightServiceTest::test_controlled_guard_skip_defers_preflight_bucket_for_retry backend/tests/test_dead_streams.py::TestDeadStreamDetection backend/tests/test_stream_stats_utils.py -q
```

Result: `39 passed`.

Repository CI helper:

```bash
python scripts/run_ci_checks.py
```

Result: `75 unittest checks OK`.

V2 backend target matrix:

```bash
python -m pytest backend/tests/test_automation_run_progress.py backend/tests/test_run_observability.py backend/tests/test_stream_checker_queue_lifecycle.py backend/tests/test_stream_checker_progress_observability.py backend/tests/test_stream_checker_single_channel_profile_flags.py backend/tests/test_single_channel_checking_mode.py backend/tests/test_teamarr_preflight_service.py backend/tests/test_viewer_activity_handlers.py backend/tests/test_current_viewers_tracking.py backend/tests/test_concurrent_stream_limiter.py backend/tests/test_ffmpeg_hardware_acceleration.py backend/tests/test_change_stream_endpoint.py backend/tests/test_proxy_status_integration.py backend/tests/test_connectivity_guard.py backend/tests/test_dead_streams.py backend/tests/test_queue_logging_accuracy.py backend/tests/test_stream_checker_core.py backend/tests/test_active_stream_detection.py backend/tests/test_stream_stats_utils.py -q
```

Result: `212 passed`.

Frontend tests:

```bash
npm.cmd --prefix frontend run test:ci -- --reporter=dot
```

Result: `19 passed`.

Frontend production build:

```bash
npm.cmd --prefix frontend run build
```

Result: passed.

Full backend pytest suite:

```bash
python -m pytest backend/tests -q --tb=short --disable-warnings
```

Result: `994 passed, 2 skipped`.

Security validation:

- GitHub Advanced Security / CodeQL comment for information exposure in `backend/apps/api/stream_checker_handlers.py` was fixed by sanitizing unexpected single-channel check failures.
- The original CodeQL review thread is now resolved/outdated on PR #430 after commit `15b5bc8`.
- Follow-up local validation after commit `c576cab`: `backend/tests/test_shadow_blank_monitor_service.py` passed (`17 passed`), Teamarr/single-channel targeted tests passed (`12 passed`), full backend suite passed (`992 passed, 2 skipped`), frontend tests passed (`17 passed`), and frontend production build passed.
- Follow-up local validation after the dashboard live-refresh, abort-state, and CPU-only hardware-status fixes: run-observability/queue/hardware targeted tests passed (`20 passed` for the latest hardware/abort target set), full backend suite passed (`994 passed, 2 skipped`), frontend tests passed (`19 passed`), and frontend production build passed.
- Follow-up local validation after ignoring disabled automation periods: disabled-period target tests passed (`13 passed`), full backend suite passed (`996 passed, 2 skipped`), frontend tests passed (`19 passed`), frontend production build passed, and GitHub PR checks were green on commit `e661598`.

## Self-Hosted Runtime Validation

V2 image was built and deployed over an existing self-hosted StreamFlow container:

- Image tag: `ghcr.io/bttfw/streamflow:teamarr-preflight-no-api-key-ui`
- Live health after deploy: healthy.
- Hardware acceleration startup logs showed CUDA mode available with CPU fallback enabled and the NVIDIA runtime/GPU visible.
- Startup UDI refresh completed with:
  - 228 channels
  - 217,192 streams
  - 6 M3U accounts
  - 226 logos

Focused automation smoke run:

- Temporary profile: one provider playlist, stream matching enabled, stream checking enabled.
- Temporary period: one channel.
- Result: completed.
- Temporary profile and period were removed after the test.
- Automation service was restarted afterward.

Observed live dashboard result:

- All automation stages completed.
- Duration boxes populated:
  - M3U Refresh
  - Cache Sync
  - Stream Matching
  - Quality Check
- No `N/A` duration regression in the completed run card.

## Final Validation Status

The requested final validation gates are complete for review:

- Full persistent-data backup and machine-readable exports were captured before destructive install testing.
- Fresh empty boot with GPU/NVIDIA runtime passthrough completed.
- Settings, profiles, periods, regex patterns, shadow monitor, Teamarr preflight, scheduling, and Dispatcharr config were restored.
- Startup UDI refresh completed after restore with 213 channels, 217,429 streams, and 6 M3U accounts.
- GPU runtime status reported CUDA mode available with CPU fallback enabled.
- Shadow Monitor restored with `skip_during_quality_check=false`; the UI no longer exposes a quality-check guard toggle for Shadow Monitor.
- A GPU-passthrough Full Check reached the quality-checking phase with no observed quality-check failures before it was intentionally stopped for the requested CPU-only/no-GPU install validation.
- A CPU-only/no-GPU fresh install and real Full Check completed on the V2 branch with 212/212 channels checked and 0 failed channels.
- The CPU-only/no-GPU hardware-status follow-up now reports `ffmpeg_available=true` while hardware acceleration is disabled and NVIDIA runtime is absent.
- The current branch image from GitHub Actions (`ghcr.io/bttfw/streamflow:teamarr-preflight-no-api-key-ui`, commit `e661598`) was deployed through a normal self-hosted container template, keeping it GUI-editable.
- Disabled automation periods were validated live on commit `e661598`: a disabled due period did not trigger catch-up automation.
- GPU-passthrough targeted quality validation on commit `e661598` completed 5/5 selected channels with 0 failed channels.
- CPU-only/no-GPU targeted quality validation on commit `e661598` completed 5/5 selected channels with 0 failed channels.
- CPU-only/no-GPU normal single-channel validation on commit `e661598` completed for `RTL UHD`: profile-driven M3U refresh, validation, rematch, quality analysis, and reorder all succeeded; 2/2 streams were analyzed with 0 failed stream probes.
- CPU-only/no-GPU normal single-channel validation on commit `e661598` completed for `12News (NBC) - KPNX - Mesa/Phoenix`: profile-driven M3U refresh, validation, rematch, quality analysis, provider-limit scheduling, freeze detection, removal, and reorder all ran; 12 streams were analyzed, 1 frozen stream was removed, and the final response was successful.
- GitHub Actions checks for the latest code head were green before this final documentation update.
