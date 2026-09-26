# PR #460 changelog (Unreleased)

This document covers the changes proposed by [PR #460](https://github.com/krinkuto11/streamflow/pull/460) against `dev`. It records this PR's work only; the existing V3 and V4 changelogs remain the record for their earlier changes. No release version is assigned here.

## Added

- Bounded monitoring-session intervals in the create APIs: `evaluation_interval_ms` accepts 100–60,000 ms and `enforce_sync_interval_ms` accepts 500–10,000 ms; both default to 1,000 ms. The Monitoring form labels the latter as the enforcement tick.
- Effective automation-profile information in compact Channel rows, with explicit loading and failure states. Channel search, group controls, ordering and pagination remain available at mobile widths.
- Six desktop/mobile screenshots of Dashboard, Channels and Monitoring under `docs/pr-screenshots/`, captured with synthetic fixture data.

## Changed

- The production and development images use the pinned LinuxServer FFmpeg/ffprobe 8.1.2 build used by the installed Dispatcharr image. Python 3.11 runs in a separate virtual environment; the production build checks the amd64 binary hashes. This aligns media analysis with Dispatcharr, without claiming that the FFmpeg version alone makes checks faster.
- Single-channel checks keep their browser request open until the backend responds, and matching evaluates only the requested channel's rules. Full runs still match against all configured channels; the stream catalogue remains the discovery input for a single-channel check.
- Dashboard puts the primary run action and current run first, keeps live playback concise and moves cache/maintenance detail into a disclosure. Monitoring uses compact session rows, accessible controls, responsive detail tables and a narrow-screen timeline. The mobile navigation is a modal drawer with focus containment, focus return, Escape handling and scroll locking.
- Application startup obtains configuration state from `/api/readiness` instead of running the setup-wizard connection diagnostic on every page load. The setup flow remains available when configuration is required.
- Settings save requests are scoped to the visible section. Failed section loads disable their save action and expose a retry instead of sending unrelated or incomplete settings updates.

## Fixed

- The reported **old streams staying in a channel** issue was reproduced in the write-back path: after ranking applied a channel stream limit, cached streams excluded by that limit were misidentified as missing from the UDI cache and appended again. Both concurrent and sequential checks now compare against the loaded cache before enforcing the final limit. Genuine unresolved cache misses remain protected, and an active viewer remains protected by the limit logic. This reproduces and fixes the identified mechanism; the original reporter's instance has not been directly verified.
- An unsuccessful matching worker batch, stale or unavailable inventory after refresh, failed validation, rejected Dispatcharr assignment, or missing quality result no longer becomes a successful automation or single-channel run. Single-channel validation (Step 4), discovery (Step 5) and quality analysis (Step 6) report failures before proceeding to later stages.
- The manual **Discover Streams** quick action accepts the manager's structured result, validates its numeric `assignment_count` mapping and returns the existing `assignments`/`total_assigned` success shape. A nonempty successful discovery no longer raises a type error and returns HTTP 500 merely because the manager supplied structured output.
- Channel assignment updates serialize StreamFlow writers per channel, read the authoritative Dispatcharr assignment before a potentially stale write and verify the ordered assignment after PATCH. Unknown stream IDs are checked against Dispatcharr before they can be removed as cache misses.
- Monitoring does not send quarantined sources back through the fallback. Eligible review sources can still be used after cooldown. Manual revival clears the dead marker, confirms the Dispatcharr assignment and persists the updated session; automatic Hidden → Unhidden recovery is confirmed while a manual hide remains respected.
- A positive FFmpeg bitrate is retained if a later progress report contains zero. Missing-bitrate recheck can be disabled without immediately retrying a completed no-bitrate probe; genuinely incomplete or failed probes keep their existing error handling. Visual checks detect truncated evidence explicitly, and interrupted FFmpeg probes drain their output before classification.
- Channels and Monitoring show explicit load/error states. Channel profile fetches follow the current page and search state; responsive navigation no longer leaves invisible keyboard targets or mishandles an unconsumed Escape key.

## Performance

- Dead-stream filtering and revival use batched or shared snapshots rather than repeated per-stream database lookups. A failed dead-state read stops matching or assignment instead of silently treating every source as usable.
- Provider inventory and automation configuration are reused within a run; channel refresh avoids a redundant stream-ID fetch. Only sufficiently fresh (at most 30 seconds old) live UDI stream metadata is reused for monitoring-session creation. Stale or uninitialized metadata still causes a full fetch.
- Idle monitoring avoids unnecessary assignment PATCHes while periodically checking Dispatcharr for external drift. Session detail reads fetch only a bounded chart window of historical telemetry when an in-memory window is absent; this does not delete stored history.
- Stream Checker uses the status response's progress field and loads mostly static settings less often. Monitoring charts and detail code load when opened. These are request and initial-route reductions, not evidence of a shorter provider probe or whole full run.

## Security

- Logo downloads now have a 4 MiB per-image limit, a 512 MiB cache budget, a deadline, limited redirects and response-type/content checks. The downloader rejects metadata and unauthorized local-service targets while retaining supported Dispatcharr-relative and LAN logo URLs.
- Cached image responses set `X-Content-Type-Options: nosniff`; SVG responses also receive a restrictive Content Security Policy. Cache writes use temporary files and atomic replacement.
- A rejected logo returns a generic HTTP 422 message rather than returning the internal rejection detail to the client. Manual stream-discovery quick actions project only validated numeric channel assignment counts into their existing success response. Aborts and failures return generic HTTP 409/500 messages and retain a safe `partial_writes` flag; provider URLs, nested statistics and exception text remain in server-side diagnostics rather than the API payload.

## Operator impact and compatibility

- The existing **Bitrate Recheck** setting remains enabled by default at **Stream Checker → Stream Checker Configuration → Stream Analysis → Bitrate Recheck** (select **Edit** to change it). This PR makes the disabled setting effective for completed no-bitrate probes; turning it off reduces optional recheck probes but does not convert a missing bitrate into a valid measurement. Blank, freeze, loop and provider-account limits remain separate analysis stages and safeguards.
- A genuinely failed run now returns failure instead of a success marker. A failure after earlier channel writes is reported as a partial-write condition where available; this change does not provide cross-application rollback.
- A quarantined source is not silently reattached merely because no stable source remains. Operators can review eligible sources or explicitly revive a quarantined source. Channel visibility recovery and manual hides follow their separate rules.
- The new FFmpeg base keeps CPU use as the container default; an explicitly configured NVIDIA runtime/device setting still enables GPU analysis. The existing Unraid Docker Manager template path remains usable.
- The monitoring interval API retains a 1,000 ms default for both new fields. Existing clients may omit them. The enforcement tick does not imply a Dispatcharr PATCH every second when the desired assignment is unchanged.

## Validation (separate from release notes)

- Backend stable and integration suites, frontend tests and production build, and amd64/arm64 image jobs passed on the previously verified PR head. Earlier CodeQL analysis completed but surfaced two medium exception-disclosure alerts in the logo and manual-discovery API paths. This follow-up changes those responses; a fresh scan is required before claiming the alerts are resolved. The [PR checks](https://github.com/krinkuto11/streamflow/pull/460/checks) remain the source for the current head.
- Local regression coverage includes worker and validation failures, stale cache/stream-limit behavior, quarantine and revival, visibility transitions, missing bitrate, FFmpeg evidence handling, logo-response limits, monitoring writes, mobile navigation and startup readiness.
- `test_preflight_preserves_channel_assignments.py` covers the stream-limit false cache miss, genuine missing IDs and active-viewer preservation for the mechanism fixed in `6b030bca`. This is a code-level reproduction, not a verification on the original reporter's Dispatcharr instance.
- The security/API-response follow-up passed 57 focused tests, and the stream-limit regression group passed 8 focused tests (65 total). The new commit still requires its own CI and CodeQL result before a release claim; the earlier passing scan applies to the earlier PR head.
- Earlier live Unraid canaries verified FFmpeg/ffprobe parity and CPU/GPU decode through the VPN-bound container, automatic hide/recovery, manual-hide preservation, quarantine and manual revival. Later real single-channel Full Check runs for 3sat HD, ARTE HD and Welt HD completed in the existing Docker Manager container; the detailed outcomes and the first test harness's missing HTTP capture are recorded in the PR body.
- A separate full multi-hour automation run and a live optional loop-source outcome are not claimed here. Matcher and database timings quoted in the PR are local path benchmarks, not whole-run performance results.

## Known limits

- Dispatcharr does not provide a conditional assignment PATCH/ETag. StreamFlow can serialize its own writers and verify readback, but an external writer can still race between preflight and PATCH.
- A stale monitoring-session cache requires a full Dispatcharr stream payload to detect changed stream names or URLs; the ID-only endpoint cannot prove those fields are current.
- Base, optional visual, missing-bitrate recheck and optional loop analysis can each start a separate FFmpeg process. `ffprobe` is a timeout fallback, not an unconditional second probe. Provider connection time depends on enabled checks and their configured durations.
- A live missing-bitrate recheck and a live optional loop source were unavailable for the earlier bounded canaries; their semantics have targeted tests. See the PR body for the latest live test scope.
