# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.5.6.1] - 2026-05-15

## [Unreleased]

### Added
- **StreamFlow V3 reliability stack** - Draft-gated work for provider/profile-aware Stream Checker capacity, Teamarr managed-event preflight, Shadow Monitor continuity, startup progress, hardware diagnostics, and in-app operator Help.
- **Detailed V3 changelog** - Added `docs/pr432-v3-changelog.md` so PR #432 has a readable branch-level changelog and validation record instead of relying on an oversized PR body.
- **Detailed V4 changelog** - Added `docs/pr434-v4-changelog.md` so PR #434 tracks the release-hardening scope, image digest, live gates, screenshots, and remaining draft blockers outside the PR body.
- **M3U priority modes** - Added `Playlist Priority -> Score` and `Score -> Playlist Priority` modes for automation profiles.
- **Automation run stream health metrics** - Added a dashboard `Good Streams` metric and a compact `Checking now` row for active quality batches.

### Changed
- **Operator-facing progress and setup wording** - Clarified Stream Checker ETA labels, dashboard run counters, startup duration estimates, Teamarr timing buckets, Shadow Monitor switch limits, and Help `Where` locations.
- **Help and setup guidance** - Keeps V3 Help platform neutral, points settings to visible UI or explicit status/API locations, and shows shipped UI screenshots as cropped, optimized, collapsible, lazy-loaded references.
- **Stream Checker ETA** - Batch and full-run ETA now uses a conservative channel-throughput floor alongside stream-level progress, preventing long full checks from reporting unrealistically short remaining time.
- **Auto-Create scheduling outcomes** - EPG regex previews and refresh results now distinguish total matches from schedulable future events, due-now queue entries, already-checked events, missing time, and invalid time so operators can tell why a large match count may leave fewer Scheduled Events visible.
- **M3U refresh observation** - Automation now waits for accepted playlist refreshes to settle before cache sync, reports waiting/settled/partial/timeout/failed states, and can retry failed providers without blocking healthy providers.
- **Synchronous full-run guard** - Long full runs keep the automation busy guard alive for production-scale quality batches instead of being treated as a stale lock after one hour.
- **Stream Checker ETA wording** - Provider-limited or floor-based estimates now display as `Rough ETA` with a tooltip explaining expected swings between long channel waits.

### Fixed
- **Missing-bitrate rechecks** - Rechecks playable streams with no current bitrate serially after each channel's initial probes, keeps failed rechecks explicitly `N/A`, and treats older bitrate history as ranking-only evidence.
- **Shadow configuration concurrency and scope** - Uses revision-guarded configuration saves, applies explicit monitor include scopes, and cancels affected probes when relevant Shadow settings change without overwriting the separately stored watcher key.
- **V3 reliability fixes** - Preserves active batch dead/blank/freeze counters, avoids misleading DRI hardware warnings, improves Teamarr post-start checks, validates auto-create group previews across channels, and keeps real-viewer wording distinct from watcher probes.
- **V3 final polish** - Adds safe profile-slot ID/limit tooltips, active Teamarr check runtime text, and explicit checker-capacity/global-worker quality reason wording.
- **Stream Checker profile matrix** - Adds an expandable provider/profile matrix with safe profile ID, effective enforced limit, active viewer, checking, free-slot, and status details. Live stream rows also show the exact profile used as safe ID/name/effective-limit telemetry without exposing probe URLs or credentials; shared credential aliases report their strict shared-route limit, released reservations clear on capacity waits or viewer preemption, and serial bitrate rechecks show the profile they actually reserve.
- **Provider capacity authority** - Treats malformed or changing account/profile inventory, credential routes, and live proxy-status usage as unavailable rather than zero; reservations, rechecks, preemption, and slot telemetry share atomic authority snapshots and isolated status-cache copies so checks fail closed without consuming a viewer slot or leaking credentials.
- **Stream Checker progress generation safety** - Fences status/progress publication and clear operations by run generation with atomic snapshots, preventing late worker callbacks from overwriting a newer run or resurrecting cleared Current Progress state.
- **Container build-tool security** - Overrides the Python base image's vulnerable vendored `setuptools` helpers with a reviewed, hash-locked release that contains fixed `jaraco.context` and `wheel` versions.
- **Teamarr event preflight override** - Adds a warned provider/profile capacity override for event checks while keeping active-viewer protection enforced.
- **Teamarr managed-event visibility** - Keeps larger managed-event schedules visible, accepts alternate Teamarr start-time/channel-id fields, exposes managed-record counts, and queues due events when direct preflight capacity is full.
- **Teamarr scheduled-event clarity** - Scheduled Teamarr events now expose the next automatic check bucket/time and label the manual event action as `Force Check`, so `Scheduled` + `No Check` cards still show the available one-time check path.
- **Hardware acceleration probe logging** - Resolves DRI `auto` analysis to a concrete VAAPI render node, logs the requested FFmpeg hardware path per probe, and leaves CUDA/NVIDIA plus explicit VAAPI/QSV paths unchanged.
- **Stream Session runtime guard** - Reinitializes volatile session refresh state on reused singleton instances so session creation does not fail after unusual import or test ordering.
- **Shadow Monitor recovery defaults** - Uses the live-validated continuous watcher defaults, enables freeze detection, disables dry-run by default when the monitor is enabled, and keeps switching rate limits in place.
- **Viewer-proxy blank recovery** - Shadow Monitor now verifies the real viewer proxy path after stream switches and does not hide a failed replacement behind the normal channel cooldown.
- **Automation catch-up policy** - Adds explicit `Run all due periods` opt-in; automatic scheduler passes now default to the highest-priority due period and only process all due periods when the operator enables it, with Catch-up cap as the load guard.
- **Stream Checker run metrics** - Keeps synchronous batch blank/freeze totals visible, counts only accepted stream assignments in matching metrics, and clears stale waiting reasons once streams start checking or probing.
- **Auto-Create rule refresh** - Multi-channel/group Auto-Create rules now match all selected EPG-backed channels, use live channel and TVG identity, avoid duplicate immediate matching, and enqueue one channel-level event check at a time with priority 90.
- **Queue and Teamarr Preflight isolation** - Direct Teamarr preflight checks, queued event checks, and synchronous quality batches share one serialized stream-check path so due event checks wait instead of overlapping or disappearing.
- **Blank/freeze result accounting** - Full-run and batch summaries keep dead, blank, and frozen classifications visible while checks are active and after finalization.
- **Mobile scheduling toolbar** - The Scheduling header and Auto-Create toolbar wrap cleanly on narrow dark-mode layouts instead of clipping action buttons.
- **Shadow Monitor status safety** - Shadow Monitor startup is gated on watcher API-key readiness, keeps viewer/EPG context visible, and avoids exposing raw backend exception detail in monitor status responses.
- **Low-quality reason visibility** - Stream Checker rows now show the low-quality reason directly under the `Low Quality` status badge instead of relying only on the stream-name subtext.
- **Shadow Monitor recovery guard** - Continuous watcher recovery now cancels pending blank/freeze confirmations when a real watcher client returns before the confirmation pass, avoiding false stream switches after transient watcher reconnects.
- **Teamarr post-start bucket handling** - Teamarr Preflight catches configured `post_start_offsets_minutes` inside the configured post-start grace window instead of hard-coding one offset or missing buckets after long serialized checks.
- **Teamarr stale stream refresh** - The `no_streams_yet` path refreshes the affected channel stream mapping before making the final decision, reducing false no-stream outcomes after Dispatcharr/Teamarr updates.
- **Teamarr Preflight loading and queue display** - The Teamarr Preflight page no longer crashes while config/status data is still loading, and queued checks running through the Stream Checker queue are shown in the active/running area with the configured concurrency limit.

### Removed
- **AceStream Monitoring** - Removed the unused AceStream Monitoring surface while keeping normal Stream Monitoring, Stream Checker, Shadow Monitor, and Teamarr Preflight intact.


## [2.5.6] - 2026-05-07

### Removed
- **SQL persistence layer removed from UDI** — `UDIStorage` previously persisted all cached data to SQLite. Because Streamflow requires a live Dispatcharr connection to function, the warm-start benefit did not justify the complexity. The UDI now operates as a pure in-memory cache repopulated from the API on every startup. `storage.py` is retained as a stub so existing imports do not break.

### Added
- **Delta sync (`refresh_delta()`)** — A lightweight delta sync method is now wired into the scheduler. It fires on every 60-second tick between scheduled full refresh slots, using the `/ids/` endpoints to detect adds and deletes cheaply without re-fetching all data. When the change ratio exceeds the threshold it falls back to `refresh_all()` automatically. When no schedule is configured the worker remains dormant.
- **Bulk stream fetch via POST** — `fetch_streams_by_ids()` now uses `POST /api/channels/streams/by-ids/` with batches of up to 500 IDs, replacing the prior N-concurrent-GET pattern.
- **`fetch_all_ids()`** — New method that fetches channel and stream ID sets concurrently in a single round-trip, used as the primary input for delta diffing and as the integrity oracle for `refresh_all()`.
- **`_post_url()`** — New internal POST helper with the same 401→refresh→retry pattern as `_fetch_url()`.

### Performance
- **Parallel entity fetching in `refresh_all()` (~2.6× speedup)** — All entity fetches now execute concurrently via `ThreadPoolExecutor` instead of sequentially. Measured on a live instance: 755 ms → 294 ms.
- **Concurrent pagination** — `_fetch_paginated()` now fetches all pages after the first concurrently (up to 10 workers), merging results in page-number order for deterministic output.
- **Concurrent profile channel fetch** — `fetch_profile_channels()` now fetches all profiles in parallel (up to 8 workers) instead of sequentially.
- **Parallelized delta fetch** — New stream and new channel fetches in `refresh_delta()` step 4 run concurrently via a 2-worker `ThreadPoolExecutor`.
- **Default page size 100 → 1000** — All paginated API calls request 1000 results per page by default, cutting round trips by ~10× for typical deployments.
- **EPG fetch reduced from N calls to 1 per cache cycle** — `fetch_channel_programs_from_api()` previously made one HTTP request to `/api/epg/programs/` per unique tvg_id, but the Dispatcharr endpoint ignores the `tvg_id` filter and returns all programs every time (SCH-001). With 31 channels this meant 31 identical 2100-program payloads fetched and discarded. A new `_fetch_and_cache_all_programs()` helper fetches the full program list once per cache window, groups results by tvg_id in memory, and serves all per-channel lookups from that single cache. `fetch_channel_programs_from_api()` retains the same public signature; callers are unchanged.

### Fixed
- **`refresh_delta()` was never called** — The delta sync method was fully implemented but the scheduler always called `refresh_all()`. Now wired into `udi_refresh_processor_loop()`.
- **Eliminated profile channels N+1** — `refresh_all()` and `refresh_channel_profiles()` each fired one GET per profile to retrieve channel ID lists already embedded in the profiles list response. Replaced with a dict comprehension over already-fetched data.
- **Redundant `_channels_by_id` rebuild** — After stripping deleted stream IDs from channel stream lists, the code rebuilt the entire `_channels_by_id` index unnecessarily. Removed — in-place `ch['streams']` mutation propagates through shared dict references.
- **Scheduler guard log noise** — Guard skip messages in `udi_refresh_processor_loop()` demoted from `INFO` to `DEBUG` (were emitting every 60 seconds).

### Fixed (scheduled EPG system)
- **Duplicate events on rule edit** — the root cause was a race between event deletion and background re-matching: events were deleted inside the lock, then the lock was released before the background thread ran, allowing a concurrent EPG refresh to re-create them. The deletion now happens inside the background thread immediately before re-matching, keeping both operations atomic under the same lock acquisition.
- **Concurrent match duplicates** — the entire `match_programs_to_rules()` loop now runs under `_lock` so two concurrent callers (EPG refresh worker + rule-edit background thread) can no longer both pass the "no duplicate exists" check and both write the same event.
- **`CHANNEL_NAME` token never matched** — the token was substituted with a placeholder only for syntax validation, but the raw unsubstituted pattern was compiled for actual matching. Rules containing `CHANNEL_NAME` silently matched nothing. It is now replaced with `re.escape(channel_name)` before compilation in both the matching pipeline and the test-regex endpoint.
- **Empty regex matches all programs** — an empty or whitespace-only `regex_pattern` is now rejected at rule create/update time and skipped with a warning during matching. Previously it compiled to a pattern that matched every program title.
- **Duplicate detection corrupted nearby events** — the previous 300-second time-window check could match a different program airing close in time and overwrite its title/times. Deduplication now uses the exact composite key `(channel_id, rule_id, program_start_time)`, eliminating false matches between nearby programs.
- **Stale `channels_info` tvg_ids** — `channels_info` embedded in a rule document was used as-is for EPG fetching, meaning a channel's tvg_id change after rule creation was silently ignored. Channel info is now always resolved live from the UDI cache at match time.
- **Invalid regex logged without rule identity** — error logs for bad patterns now include the rule name and the effective pattern (post `CHANNEL_NAME` substitution) so the broken rule is immediately identifiable.
- **Creating/updating auto-create rules timed out and queued Waitress workers** — `create_auto_create_rule` and `update_auto_create_rule` both called `match_programs_to_rules()` synchronously on the request thread. That function holds the scheduling lock and fires HTTP calls to Dispatcharr for every channel's EPG data, routinely taking 30+ seconds. The 30-second axios timeout on the frontend caused the create/update to appear failed even though the rule had already been saved. Waitress logged "task queue depth is 1" because all worker threads were stalled on these calls. Matching is now fired in a daemon background thread for create; for update, the redundant handler-level call is removed since `update_auto_create_rule()` already spawns its own background match thread. Both endpoints now return immediately after saving.
- **EPG rule dialog footer scrolled out of view** — the rule creation/edit dialog used `overflow-y-auto` on the full `DialogContent`, causing the Cancel and Create Rule buttons to scroll off-screen when the form was tall (e.g., with AceStream monitoring settings expanded). The dialog is now a flex column with scroll confined to the form body only, keeping the footer always visible.
- **EPG rule regex preview showed past/airing programs** — `test_regex_against_epg` returned all matching EPG programs, including those that had already started or finished. The preview now filters out programs whose `start_time <= now`, matching the behaviour of `match_programs_to_rules()` exactly. Users will no longer see matches in the test panel that the rule would never actually schedule.

### Fixed (automation profile/period handling)
- **Stale profile reference warning** — `get_profile()` now logs a `WARNING` when a referenced profile ID does not exist in the database, making stale assignments visible in the logs instead of silently returning `None`.
- **Assignment validation** — `assign_period_to_channels()` and `assign_period_to_groups()` now validate that the target profile exists before writing. An invalid profile ID is rejected with an `ERROR` log and returns `False`, preventing stale references from accumulating.
- **Assignment race condition** — both assignment methods now hold `self._lock` around the read-modify-write of the config dict, preventing concurrent assignments from losing each other's writes.
- **Period priority tie-break** — when multiple periods share the same `priority`, they are now sorted by numeric period ID instead of lexicographic string comparison (e.g. period `9` no longer sorts after `10`).
- **Invalid cron expression** — a malformed cron expression on a period now causes that period to be **skipped** with an `ERROR` log (including the bad expression value) instead of silently falling back to a 60-minute interval. Same behaviour when `croniter` is not installed.
- **Dead stream removal safe default** — `dead_stream_removal_enabled` is now initialised to `False` in `_check_channel_concurrent()`, `_check_channel_sequential()`, and the single-channel check step 5 fallback. If profile resolution fails, streams are left in place rather than silently removed.

### Refactored
- **Auth consolidation in upload modules** — `channels_upload.py` and `groups_upload.py` each contained ~100 lines of duplicated auth logic without the thread-safety lock present in `apps.core.auth`. Both now import from `apps.core.auth` directly, eliminating a concurrent token-refresh race condition.
- **`total_steps` reduced from 7 to 6** — The `fetch_profile_channels()` step is no longer a separate tracked step in the init progress reporting.

## [2.5.1] - 2026-03-17
### Added
- Initial release tracked in this changelog.
- Fixed Clear Queue button not working by adding abort function.
