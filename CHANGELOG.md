# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Refactored
- **Auth consolidation in upload modules** — `channels_upload.py` and `groups_upload.py` each contained ~100 lines of duplicated auth logic without the thread-safety lock present in `apps.core.auth`. Both now import from `apps.core.auth` directly, eliminating a concurrent token-refresh race condition.
- **`total_steps` reduced from 7 to 6** — The `fetch_profile_channels()` step is no longer a separate tracked step in the init progress reporting.

## [2.5.1] - 2026-03-17
### Added
- Initial release tracked in this changelog.
- Fixed Clear Queue button not working by adding abort function.
