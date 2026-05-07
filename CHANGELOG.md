# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Performance
- **EPG fetch reduced from N calls to 1 per cache cycle** — `fetch_channel_programs_from_api()` previously made one HTTP request to `/api/epg/programs/` per unique tvg_id, but the Dispatcharr endpoint ignores the `tvg_id` filter and returns all programs every time (SCH-001). With 31 channels this meant 31 identical 2100-program payloads fetched and discarded. A new `_fetch_and_cache_all_programs()` helper fetches the full program list once per cache window, groups results by tvg_id in memory, and serves all per-channel lookups from that single cache. `fetch_channel_programs_from_api()` retains the same public signature; callers are unchanged.

### Fixed
- **Creating/updating auto-create rules timed out and queued Waitress workers** — `create_auto_create_rule` and `update_auto_create_rule` both called `match_programs_to_rules()` synchronously on the request thread. That function holds the scheduling lock and fires HTTP calls to Dispatcharr for every channel's EPG data, routinely taking 30+ seconds. The 30-second axios timeout on the frontend caused the create/update to appear failed even though the rule had already been saved. Waitress logged "task queue depth is 1" because all worker threads were stalled on these calls. Matching is now fired in a daemon background thread for create; for update, the redundant handler-level call is removed since `update_auto_create_rule()` already spawns its own background match thread. Both endpoints now return immediately after saving.
- **EPG rule dialog footer scrolled out of view** — the rule creation/edit dialog used `overflow-y-auto` on the full `DialogContent`, causing the Cancel and Create Rule buttons to scroll off-screen when the form was tall (e.g., with AceStream monitoring settings expanded). The dialog is now a flex column with scroll confined to the form body only, keeping the footer always visible.
- **EPG rule regex preview showed past/airing programs** — `test_regex_against_epg` returned all matching EPG programs, including those that had already started or finished. The preview now filters out programs whose `start_time <= now`, matching the behaviour of `match_programs_to_rules()` exactly. Users will no longer see matches in the test panel that the rule would never actually schedule.

## [2.6.0] - 2026-05-07

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
