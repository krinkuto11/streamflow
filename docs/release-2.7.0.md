# StreamFlow 2.7.0 — 2026-10-08

This release promotes the tested development branch to `main`. Changes below are relative to **2.6.0.1** and include PRs [#460](https://github.com/krinkuto11/streamflow/pull/460), [#461](https://github.com/krinkuto11/streamflow/pull/461), [#462](https://github.com/krinkuto11/streamflow/pull/462), [#463](https://github.com/krinkuto11/streamflow/pull/463), [#464](https://github.com/krinkuto11/streamflow/pull/464), and the OpenStream monitoring updates.

## Added

- **Optional playback stability history.** StreamFlow can observe existing Dispatcharr viewer sessions and persist observed delivery time, stalls and source switches in SQLite without opening another provider connection. History survives restarts, has configurable retention, and ties evidence to a stream ID and source-URL hash. Viewer identities, IP addresses, credentials and source URLs are not stored in this history.
- **Optional stability deductions in quality scoring.** Enable collection at **Settings → Monitoring → Playback Stability → Record Playback Stability**, then opt individual profiles in at **Profile editor → Stream Checking → Stream Quality Scoring → Use Playback Stability**. Both options default to **off**. A source needs at least ten observed minutes and 60 valid samples before its history can affect scoring. Sources without enough evidence retain their existing score; stable sources receive no deduction. The default optional weight is 15%. Ordinary and Teamarr channels follow the same rules, with independently configured profiles.
- **Playback history controls.** Polling can be set to 5–60 seconds (default 10), retention to 1–30 days (default 14). A sanitized history table shows observed time, stall time, failovers and a score or `Not enough data`.
- **Matrix appearance.** A black/charcoal theme with green accents joins Light, Dark and Auto in the appearance menu. Selection persists across reloads, while warning/error colors retain their meaning.
- **Channels Restored quick metric.** Dashboard now displays restored channels beside checked channels, good streams, dead streams and hidden channels, with responsive wrapping.
- **OpenStream monitoring sessions.** Monitor supported AceStream sources through an OpenStream server's API and swarm telemetry. The session UI exposes resolution, frame rate, delivery health and latency. Settings → Connection → OpenStream provides API-key configuration and connection testing; environment and secret-file configuration are also supported.
- **Monitoring interval controls.** Session creation accepts bounded evaluation and enforcement intervals, both defaulting to 1,000 ms. The enforcement tick does not force an assignment write on every tick.

## Changed

- **Responsive workspace.** Dashboard, Channels and Monitoring use more compact layouts, explicit loading/error states, effective profile information, and accessible mobile navigation with focus handling and Escape support. Monitoring details and charts load when opened.
- **Faster startup path.** Configured instances use application readiness for startup instead of rerunning the setup connection diagnostic on every page load. Initial setup remains available when needed.
- **Safer settings saves.** Saves apply to the relevant settings section. Failed section loads expose retry controls and cannot overwrite unrelated settings.
- **Preflight checkpoint handling.** Teamarr Preflight catches up to the newest crossed checkpoint after a slow scan, retries temporarily missing streams within bounded windows, and rechecks queued event/channel identity, policy and expiry before media work. Transient source outages release the attempt marker for a later retry.
- **Separate preflight catalog work.** Dropdown/catalog refresh runs independently from due-check admission. Subscription/sport/league metadata uses a short cache with configuration invalidation; event readiness still uses current source information.
- **Less repeated API and database work.** Reuse thread-owned HTTP connections, coalesce simultaneous channel reads, reuse provider/configuration snapshots within a run, batch dead-stream lookups, and avoid unnecessary monitoring writes and redundant channel-refresh reads.
- **Lighter UI updates.** Dashboard, Stream Checker and Preflight serialize polling, pause it on hidden tabs, and refresh on return. ETags reuse unchanged status responses. Large stream tables render their visible rows with overscan; countdowns update independently. Narrow screens keep readable columns through horizontal scrolling.
- **OpenStream shared polling and leases.** Sources on the same server share a keep-alive poller. Supporting servers use renewable leases, so stopping a monitoring session releases its ownership without stopping another viewer or session. Older servers retain their previous fallback behavior.
- **OpenStream ranking and recovery.** Rank sources by received delivery over a five-minute window. Dead sources rank last immediately but require 300 continuous dead seconds before quarantine. API refusals and handled polling failures appear as status rather than causing continuous monitor restarts. FFmpeg-specific speed quarantine does not apply to OpenStream telemetry.
- **Pinned media tools.** Production and development containers use a reviewed LinuxServer FFmpeg/ffprobe 8.1.2 build, with Python 3.11 in a separate virtual environment. CPU analysis remains the default; configured NVIDIA runtime/device options remain usable.
- **Stream Checker decomposition.** Queue, statistics, ownership, inventory, classification, status, and sequential/concurrent execution now live in focused modules behind the existing service facade. Shared state, lock ordering and public behavior are preserved. Bounded timing summaries separate queue/provider waits, analysis, API reads and writes.

## Fixed

- Enforce configured channel stream limits after protecting genuine cache misses and active-viewer streams. Streams intentionally removed by ranking are no longer appended again as false cache misses.
- Report matching, inventory, validation, missing-result and rejected Dispatcharr write failures as failures. Single-channel checks stop before later stages when an earlier stage fails, and discover only against that channel's rules.
- Serialize StreamFlow assignment writers per channel, check current assignments before writing, and confirm ordered assignments after PATCH. Partial writes remain explicit; external writers can still race because Dispatcharr does not offer conditional assignment PATCHes.
- Return the expected counts after successful manual Discover Streams instead of treating a structured success result as an error.
- Exclude quarantined sources from monitoring fallback. Confirm manual revival and automatic hidden-channel recovery, clear dead markers before revival, and preserve manual channel hides.
- Detect stream name, URL, group, provider and assignment changes even when IDs remain unchanged. Reject incomplete fresh metadata before probing or publishing stale cache/index information.
- Preserve positive measured bitrate across trailing zero progress. Respect the existing Bitrate Recheck switch for completed no-bitrate probes, drain interrupted FFmpeg output, and handle truncated visual evidence explicitly.
- Reconcile ambiguous channel creation responses through read-only confirmation instead of automatically replaying uncertain non-idempotent requests.
- Keep the legacy sequential retry's original quality weights when no playback evidence exists. Observation gaps, normal retuning/endings, API outages and byte-counter resets do not create false playback failures.
- Bound legacy in-memory changelog growth and idle telemetry/history reads. Preserve stored SQL history and existing viewer protection.
- Preserve Matrix active-navigation colors after the Tailwind migration, including desktop/mobile dialogs and theme switching.

## Security

- Upgrade Tailwind CSS **3.4.18 → 4.3.3**, add its official PostCSS integration, update React Router DOM **6.30.4 → 7.18.4** and tailwind-merge **2.6.0 → 3.7.0**, and remove the vulnerable legacy build dependency chain. Update source-map-js to **1.2.2**.
- Update hash-locked production/test dependencies to **Werkzeug 3.1.9** and **urllib3 2.8.0**. Frontend and production Python dependency audits pass without suppressions or disabled gates on the tested development revision.
- Bound logo downloads by image size, total cache budget, timeout and redirects; validate response content and destinations. Cached image responses set security headers, and writes are atomic.
- Return generic logo/discovery errors and validated numeric discovery results instead of exposing internal exception details or nested provider data to clients.

## Upgrade notes and limits

- Container configuration, persistent data paths and existing automation profiles remain supported. Playback collection and profile scoring require explicit opt-in; upgrading does not enable them globally.
- **Browser requirements:** Tailwind 4 requires Safari **16.4+**, Chrome **111+**, or Firefox **128+**. Existing custom fonts, semantic colors and Light/Dark layouts are preserved.
- Passive stability observes delivery through Dispatcharr. It cannot detect a decoder freeze while bytes continue arriving, identify every interruption between polls, or prove the cause of a source switch.
- Reduced duplicate work and UI requests do not establish a measured whole-run speedup. Provider limits and configured media-analysis durations still determine a substantial part of runtime.
- No Teamarr or Dispatcharr code modifications are required. Unraid deployments retain the normal GUI-editable DockerMan template workflow.

## Validation and references

- Current merged `dev` revision `31b3d5be7202c7ba86b54d9bb429e8a77006e537` passed the backend stable suite, integration contracts, frontend audit/tests/build and both CodeQL language jobs. Its amd64/arm64 container build also passed.
- Runtime source matches the previously validated combined image. Live Unraid validation covered readiness, persisted settings, passive viewer-history collection, navigation, desktop/mobile dialogs and Light/Dark/Matrix appearance. A scheduled 222-channel quality run and normal Teamarr Preflight runs completed during the preceding validation.
- Technical records: [Reliability/UI](https://github.com/krinkuto11/streamflow/blob/main/docs/pr460-changelog.md), [Preflight and checker split](https://github.com/krinkuto11/streamflow/blob/main/docs/dev-efficiency-changelog-20261002.md), [Playback stability](https://github.com/krinkuto11/streamflow/blob/main/docs/dev-playback-stability-changelog-20261007.md), [Dependency migration](https://github.com/krinkuto11/streamflow/blob/main/docs/dev-frontend-security-changelog-20261007.md), [Checker architecture](https://github.com/krinkuto11/streamflow/blob/main/docs/stream-checker-architecture.md), and [OpenStream monitoring](https://github.com/krinkuto11/streamflow/blob/main/docs/stream-monitoring.md).

**Full comparison:** https://github.com/krinkuto11/streamflow/compare/2.6.0.1...2.7.0
