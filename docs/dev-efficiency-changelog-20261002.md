# Dev reliability and efficiency work - 2026-10-02

Status: draft implementation, deployed and validated on Unraid. Post-split
backend/frontend suites, image builds and native DockerMan deployment passed.
Readiness, UI, conditional polling and a real Teamarr preflight completed on
the new image. Validation limits are below.

Base: `ccab14e7f61a12ba4fa1375e7db8472757c941bd` (`upstream/dev`).

## Compatibility

- Changes are contained in StreamFlow and use existing Teamarr/Dispatcharr APIs.
- Regex matching behavior and priorities are preserved.
- Specialized checks retain their existing maximum of 25 entries per drain.
- Media measurement reuse remains unchanged.

## Implementation ledger

| # | Change | Implementation status |
|---|---|---|
| 1 | Preflight cadence and crossed checkpoints | Implemented; regression tests passed |
| 2 | Bounded retry when a channel has no streams yet | Implemented; regression tests passed |
| 3 | Validate queued preflight source, channel identity, and expiry | Implemented; regression tests passed |
| 4 | Separate dropdown catalogs from due-check admission | Implemented; regression tests passed |
| 5 | Cache subscription/sport/league metadata | Implemented; regression tests passed |
| 6 | Thread-owned HTTP connection reuse | Implemented; regression tests passed |
| 7 | Operation-aware retries and ambiguous POST handling | Implemented; regression tests passed |
| 8 | Coalesce concurrent UDI channel refreshes | Implemented; regression tests passed |
| 9 | Detect metadata drift with unchanged IDs | Implemented; regression tests passed |
| 10 | Monotonic duration/timeout clocks | Implemented; regression tests passed |
| 11 | Conditional status responses with ETags | Tests and live 200/304 checks passed |
| 12 | Visible, serial, cancellable browser polling | Implemented; regression tests passed |
| 13 | Isolated countdowns and visible stream rows | Tests and browser fixtures passed |
| 14 | Consolidate suitable stats write paths | Implemented; regression tests passed |
| 15 | Bound legacy in-memory changelog | Implemented; regression tests passed |
| 16 | Split checker orchestration and supporting responsibilities | Implemented; regression tests passed |
| 17 | Separate operation timing summaries | Implemented; regression tests passed |

## Implementation changes

- Preflight scheduling now uses a monotonic start-to-start cadence with a wake
  signal for configuration changes and stop requests. The newest crossed timing
  bucket can be admitted after a slow scan without replaying superseded buckets.
- Candidates are reclassified after network reads against the current event time.
- Missing streams get bounded, interval-spaced retries within that bucket's
  deadline, instead of immediately consuming the attempted-bucket marker.
- Dropdown options refresh on a separate bounded worker. Catalog/subscription
  reads share a 300-second monotonic TTL cache scoped to connector credentials.
  Configuration changes invalidate it. Event/team readiness is never TTL cached.
- Queue metadata includes expiry and an enqueue monotonic timestamp. Queued
  validation checks the queued policy fingerprint (excluding the API key) and
  re-reads Teamarr
  identity and Dispatcharr channel UUID using existing
  control-plane endpoints, and records source outages separately from expiry.
  Invalid time, changed policy/source, disabled preflight, filtering, and channel
  reuse have explicit skip reasons. Explicit scans and manual checks retain
  their opt-in behavior while automatic scanning is disabled. Transient source outages release the attempt
  marker for readmission; expired checkpoints remain terminal.
- Persistent control-plane sessions belong to the current thread and origin.
  Headers stay per request; cookies do not carry across API operations. No hidden
  HTTP-adapter retries are enabled. PATCH retries distinguish permission failures
  from transient transport/server/rate-limit errors, with bounded backoff and
  numeric Retry-After handling. Authentication is refreshed at most once per call.
- Ambiguous non-idempotent POST outcomes are not replayed automatically. Callers
  can supply a read-only reconciliation callback. Channel creation reconciles
  new channel IDs against a pre-request baseline and accepts only one matching
  channel; an unconfirmed creation or refresh remains uncertain without replay. Connection-establishment
  failures and rejected authentication are distinguished from accepted requests.
- Status ETags cover the complete serialized public JSON. This saves transfer
  and frontend work on unchanged responses, not the number of status polls or
  the backend's normal status generation. Responses are private and revalidated.
- The legacy compatibility changelog retains at most 200 entries in RAM. SQL
  telemetry remains the historical record.

- Concurrent reads for the same UDI channel share only in-flight requests. A
  later check fetches fresh channel and stream metadata through existing APIs.
  Incomplete stream responses are rejected before publication.
- Stable-ID changes in names, URLs, groups, provider assignment, or channel
  assignment invalidate affected checked-stream immunity. UDI updates keep URL,
  account, and group indexes consistent. Statistics writes update existing
  indexed records without a full stream-list scan, and acknowledged local
  statistics changes during a metadata read are preserved.
- Queued preflight validation is wired into the specialized execution module.
  Direct preflight checks also publish the dedicated preflight run mode and
  reject a changed policy or expired checkpoint before launching media work.
- Single-stream statistics writes share the existing acknowledged batch writer.
  Each stream still uses Dispatcharr's existing PATCH endpoint; this is not a
  server-side bulk-write API. Cache publication follows successful responses;
  partial failures remain counted separately. Payload filtering is preserved
  independently for single-stream and batch preparation.
- Queue execution, statistics preparation/writing, channel orchestration and
  supporting responsibilities now live in separate modules, with shared
  probe-report field definitions. The initial queue/stats split at `ce7881e0`
  left a 10,862-line checker service; the subsequent decomposition replaces it
  with a small facade and the focused package documented below.
- Recovery deadlines, provider waits, media-probe elapsed time, and channel/run
  durations use monotonic clocks. Event dates and persisted start timestamps
  continue to use calendar time.
- Bounded operation summaries expose queue wait, provider wait, analysis,
  metadata/API reads, and API write request durations separately.
  Each phase retains at most 100 samples; totals cover that retained window.
  Provider wait includes provider/profile/global capacity admission. These are
  backend status fields and do not add user settings.
- The browser revalidates eligible status responses with ETags and reuses the
  prior payload for 304 responses. Its cache is bounded to 32 entries and is
  invalidated around mutations and authentication failures. Unsupported status
  endpoints retain ordinary GET behavior.
- Dashboard, Stream Checker, and Teamarr Preflight polling pause on hidden tabs,
  cancel reads on hide/unmount, serialize requests, and refresh on return.
- Stream countdowns update isolated cells. Tables with at least 100 streams
  render the visible range plus overscan, measuring variable row heights. A
  minimum table width keeps columns readable on narrow displays; horizontal
  scrolling exposes all columns without overlapping cell contents.

## Validation corrections - 2026-10-02

- Restored the complete queue terminalization callback and abort-isolation block
  inside the extracted queue mixin. Python compilation and queue lifecycle tests
  now cover the complete module rather than its partial extraction.
- Failed or incomplete fresh metadata reads now defer checks before profile
  fallback, checked-stream immunity, media analysis, or assignment writes.
  Teamarr attempt markers are released for this transient deferral.
- Newly assigned streams establish their statistics baseline before the stream
  read: fresh server values replace old cached statistics, while acknowledged
  writes during that read remain preserved.
- Catalog publication uses a generation fence. Stop/configuration changes reject
  old in-flight catalogs; a subsequent explicit scan can publish fresh dropdown
  options while automatic scanning remains disabled.
- HTTP timeout/retry tests mock the shared transport boundary. Queue ownership
  fixtures explicitly accept queued validation; dedicated expiry/source tests
  continue exercising the real validator. Post-start catch-up is tested inside
  the grace deadline rather than at its expiry boundary.

- Updated only `urllib3` from 2.7.0 to 2.8.0 in the production and test
  hash locks to resolve PYSEC-2026-4175, PYSEC-2026-4176, and PYSEC-2026-4177
  reported by the dependency audit. Wheel/source hashes come from the
  [official release metadata](https://pypi.org/project/urllib3/2.8.0/).

- Fixed overlapping stream-table columns on narrow displays. The table retains
  an 800-pixel minimum width with horizontally scrollable content and adjusted
  column proportions; the final score column remains reachable at 390 pixels.

## Recorded validation

Local environment: Windows, Python 3.12.10, Node 24.15.0. Production image:
Linux amd64, built through the existing multi-platform image workflow.

- Backend compilation: passed (`python -m compileall -q backend/apps`).
- Targeted regression/preflight/connectivity tests: 123 passed.
- Isolated integration contracts: 57 passed, 1 skipped.
- Complete stable backend suite: 1,984 passed, 1 skipped.
- Complete non-live backend suite with the locked `urllib3` 2.8.0:
  2,041 passed, 1 skipped, 1 live test deselected.
- Frontend: 295 tests passed across 39 files; production build passed, including
  the narrow-table correction.
- Python production dependency audit: no known vulnerabilities reported.
- Frontend dependency audit: high-severity gate passed; two existing moderate
  React Router findings remain. No forced major-version upgrade is included.
- PR checks at `6a367c35`: backend stable/integration, frontend, and both CodeQL
  languages passed. Image build
  [36982311277](https://github.com/bttfw/streamflow/actions/runs/36982311277)
  passed for amd64 and arm64.

### Initial live deployment at `6a367c35`

- Updated the existing container through Unraid's native DockerMan
  `update_container` routine, using its existing user template. Only the image
  repository/tag changed; network attachment, mounts, environment hash, and
  NVIDIA runtime matched the pre-update configuration. No additional container
  was installed. The previous template/image and a consistent SQLite/application
  data backup were retained before the update.
- Container became healthy; readiness reached 250/250 channels and
  198,320/198,320 streams after the initial refresh (87.183 seconds).
- Existing Dashboard, Stream Checker, and Teamarr Preflight pages loaded without
  browser exceptions. Teamarr source/catalog reads completed with no current
  preflight error. Connector configuration was not modified.
- Live progress, preflight, Shadow Monitor, and UDI-refresh status endpoints
  returned empty-body 304 responses for matching ETags. Changing checker status
  returned 200, preserving the current payload.
- Query-only diagnostics inside the existing container refreshed three stream
  records through the configured connector. A deliberately stale local URL was
  corrected with its indexes. Real queued-source validation accepted a current
  entry; isolated expired/reused-identity fixtures skipped as expected. An
  injected source timeout deferred the entry and released its attempt marker.
  These diagnostics performed no configuration, assignment, or statistics writes.
- Browser response fixtures exercised 500, 50, and 100 stream rows. The 500-row
  table rendered 16 rows at the initial and bottom positions, while the 50-row
  table rendered all rows. Countdowns advanced, conditional polls reused 304
  payloads, and a 390-pixel viewport reached the score column by horizontal
  scrolling without overlapping columns. Fixture progress was browser-local.

### Final image validation at `ce7881e0`

- Multi-platform image build
  [36985654777](https://github.com/bttfw/streamflow/actions/runs/36985654777)
  passed. Backend stable/integration, frontend, and both CodeQL language checks
  passed in the [PR workflow](https://github.com/krinkuto11/streamflow/actions/runs/36985642265).
- DockerMan updated the same existing container from its unchanged template to
  `ghcr.io/bttfw/streamflow:streamflow-efficiency-wip-20261002`. The running image
  revision is `ce7881e04d0a46e7f6d13e1709e7f9b29823f95a`.
- The container is healthy and managed by DockerMan. Host configuration, mounts,
  network attachment, and environment values match the pre-update snapshot;
  environment comparison accounts for DockerMan changing the order of entries.
  Readiness completed with 250 channels and 198,320 streams in 83.803 seconds.
- Repeated page loading and the 500/50/100-row browser fixture passed against the
  deployed frontend, including the 800-pixel table width and reachable score
  column at a 390-pixel viewport. No browser exceptions were observed.
- Repeated status checks preserved changing 200 payloads and unchanged empty-body
  304 responses. Preflight is running without a current service/Teamarr error.

## Regression specifications added or updated

Executed locally during draft validation:

- `backend/tests/test_efficiency_primitives.py`: in-flight sharing, failure
  recovery, catalog TTL/copy semantics, invalidation during reads, crossed
  checkpoints, deadline calculation, and full-response ETag revalidation.
- `backend/tests/test_operation_aware_posts.py`: ambiguous response suppression,
  reconciliation, connect/auth retries, rate limits, permission failures, and
  per-stream partial statistics write failures.
- `backend/tests/test_fresh_channel_metadata.py`: stable-ID drift, index
  consistency, incomplete responses, fresh later reads, and concurrent stats.
- `backend/tests/test_queued_preflight_validation.py`: expiry before/after reads,
  channel reuse, source changes/outages, policy changes, invalid times, and
  queue integration that prevents probes on skipped entries, bounded missing
  stream retries, and release of attempt markers after source outages.
- `frontend/src/lib/conditional-status.test.js`: 304 reuse, auth isolation,
  bounded storage, mutations, and in-flight invalidation.
- `frontend/src/lib/visible-poller.test.js`: hide/return cancellation and
  serialization, hidden-tab behavior, and recovery after transient errors.
- `frontend/src/lib/virtual-rows.test.js`: variable-height ranges, overscan,
  empty data, and scroll extent.
- Existing preflight/checker statistics tests and provider fixtures are adapted
  to the extracted writer and fresh metadata boundary.

## Dashboard quick-metric follow-up - 2026-10-02

- Added `Channels Restored` immediately after `Channels Hidden` in the always
  visible run overview. It uses the existing `ready`/`channels_ready` metric and
  the same run/source selection as the expanded detail cards; it counts channels
  actively restored during that run rather than all currently visible channels.
- The overview uses five columns on wide screens, three on medium screens, and
  two on narrow screens. No backend or connector contract changed.
- Dashboard count/display tests: 50 passed; frontend production build passed.
  Browser fixtures checked a restored value of 3 and its update to 0, five
  metrics in one desktop row, and three wrapped mobile rows at 390 pixels,
  without browser exceptions or clipped labels.
- The follow-up image built successfully for amd64/arm64 in
  [run 36987926439](https://github.com/bttfw/streamflow/actions/runs/36987926439),
  at code revision `0715f956`. This UI follow-up is included in the deployed
  checker-split image at `756c3b83`. The desktop/mobile fixtures passed again
  against that image, including restored values 3 and 0.

## Checker decomposition follow-up - 2026-10-02

- Reduced `stream_checker_service.py` from 10,862 to 907 lines by moving 113
  methods into the `apps.stream.checker` package. The facade retains construction,
  lifecycle/worker dispatch, configuration updates, and its public singleton.
- Queue ownership, capacity/inventory, classification, bitrate rechecks,
  connectivity, status/changelog, run snapshots, and each execution mode have
  focused modules; their behaviors share the original service state and locks.
- Kept callable identity and facade dependency bindings, the patchable heartbeat
  interval, original local imports, and version-file lookup semantics intact.
- Moved 15 concurrent progress, heartbeat and serial bitrate callbacks into
  three factory modules. The parallel orchestrator falls from 2,072 to 1,484
  lines; callback factories share the original counters, maps and locks.
- Source-AST equivalence passed for the relocated method bodies after reversing
  dependency access; all 15 relocated callback bodies also compare identically.
  Twenty checker submodules import successfully in fresh processes.
- Post-split validation: 215 targeted regressions passed; the complete non-live
  backend suite passed 2,041 tests, with 1 skipped and 1 live test deselected.
  All 295 frontend tests passed across 39 files; production build and complete
  backend compilation passed.
- Backend stable/integration, frontend and both CodeQL checks passed at
  `756c3b83` in [PR test run 36992134059](https://github.com/krinkuto11/streamflow/actions/runs/36992134059)
  and [CodeQL run 36992134065](https://github.com/krinkuto11/streamflow/actions/runs/36992134065).
- The amd64/arm64 image built successfully at `756c3b83` in
  [run 36992149399](https://github.com/bttfw/streamflow/actions/runs/36992149399).
- An isolated process in the existing Unraid container loaded the new source
  from a temporary directory without changing the running application. All 20
  checker submodules imported; callable identity, deep progress snapshots,
  stale/aborted publication rejection, released-profile cleanup, shared recheck
  locks and heartbeat start/stop checks passed. No media probe was launched.
- Query-only diagnostics with the new source read three existing stream
  records, repaired deliberately stale local metadata/indexes, accepted a
  current queued source, skipped expiry/UUID reuse, and deferred an injected
  source timeout while releasing its attempt marker. Configuration access used
  read-only SQLite; no configuration, assignment or statistics writes occurred.
- Subsequent native DockerMan deployment and runtime checks passed as recorded
  below. Boundaries are documented in
  [Stream Checker architecture](stream-checker-architecture.md).

### Deployed split validation at `756c3b83`

- The existing container was updated through Unraid's native DockerMan updater
  using its unchanged GUI-editable template. A fresh consistent SQLite/config
  backup and template backup were created beforehand. The running image revision
  is `756c3b83968b9d88cf94843de0d2123621f69e36`; it is healthy and DockerMan-managed.
- Host configuration, network attachment, mounts, environment values and NVIDIA
  runtime match the pre-update snapshot. No additional container was installed.
- Readiness returned 200 after startup loaded 239/239 channels and
  198,323/198,323 streams in 85.930 seconds. All required workers reported ready,
  including Stream Checker, automation and Teamarr Preflight.
- Hashes of the facade and all 21 checker package files in the running image
  match the reviewed source. Diagnostics using those installed modules passed
  callable identity, snapshot/publication guards, shared locks and heartbeat
  shutdown. Query-only checks read five real stream records and passed fresh
  metadata/index repair and queued-source validation without configuration,
  assignment or statistics writes.
- A normal Teamarr preflight completed on the deployed image: telemetry run
  `6496`, channel `11724`, seven streams, 89 seconds, `job_outcome=completed`.
  Runtime logs also show the parallel scheduler and media-analysis path; no
  NameError, ImportError, ModuleNotFoundError or AttributeError was observed.
- Dashboard, Stream Checker and Teamarr Preflight loaded without browser
  exceptions. Fixtures against the deployed frontend passed bounded 500-row
  rendering (16 visible rows), the 50/100-row cases, advancing countdowns,
  conditional 304 reuse, and reachable mobile columns at 390 pixels.
- Dashboard fixtures confirmed five quick metrics in one desktop row and three
  wrapped rows on mobile, with `Channels Restored` changing from 3 to 0. These
  fixture values are synthetic; the production counter retains existing run
  semantics.
- Five live status endpoints preserved changing 200 payloads and unchanged
  empty-body 304 responses. Teamarr Preflight reported no current service or
  Teamarr error. The regular automatic run continued after restart.

## Validation limits

- Native browser hide/return transitions could not be reproduced in this test
  environment; cancellation, serialization, and return behavior passed the
  dedicated poller tests. Live page loading and conditional polling were checked
  with the available standalone browser.
- Missing-stream retries, delayed checkpoint admission, ambiguous POST handling,
  provider admission, and partial write failures were covered by regressions and
  isolated contracts. No forced destructive connector write or live provider
  failure was introduced for this validation.
- Operation timing samples are instrumentation. The balance between added fresh
  metadata reads, fewer duplicate reads, and connection reuse has not been
  benchmarked against representative production workloads; no measured speedup
  is claimed.
- The initial 2026-10-02 live validation covered startup, UI and a completed
  seven-stream preflight. The subsequent completed 222-channel quality run is
  recorded in the 2026-10-03 full-run audit below.

## Matrix appearance - 2026-10-03

- Additional optional `matrix` appearance uses neutral black/charcoal surfaces
  and green primary/focus colors. Theme selection retains the existing
  `localStorage.theme` persistence; its effective mode remains `dark`.
- Theme-scoped colors fill the active navigation item and remove the blue
  body gradient. Switching to Light, Dark or Auto removes the Matrix class.
- UI structure, radius, spacing, status meanings, polling and backend behavior
  are unchanged. Existing semantic warning/error/information colors remain.
- Frontend verification: 295 tests passed across 39 files; production build
  passed. Browser checks of the built UI with read-only live API data passed
  47 checks covering selection pairs, reload persistence, Auto system changes,
  Matrix isolation, computed colors, menu closure, layout axes and mobile
  navigation. No browser exceptions or backend mutation requests occurred.
- The [amd64/arm64 image build](https://github.com/bttfw/streamflow/actions/runs/37107403968)
  passed at `bf5ddba644c15df49bafa7f7c55e8319927acf84`. Native DockerMan updated
  the existing container through its unchanged GUI-editable template after
  consistent SQLite/config and template backups. The actual image revision
  matches; the container is healthy and ready. Host configuration, network,
  mounts and environment values match the pre-update snapshot.
- All 47 checks passed again against the deployed frontend. Four pages and
  desktop/mobile theme selection loaded without page exceptions or backend
  mutation requests. Five visually reviewed, IP-masked live screenshots are
  stored under `docs/pr-screenshots/` and linked from the theme details.
- Backend stable CI passed 1,984 tests (1 skipped, 58 deselected); 57 isolated
  integration contracts and both CodeQL languages passed. The
  [frontend CI job](https://github.com/krinkuto11/streamflow/actions/runs/37107370737/job/111158435690)
  failed at its existing dependency-audit gate before test/build steps. The
  newly reported high-severity [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
  affects the existing Tailwind 3 build dependency chain; no patched version
  was listed at validation time. Local frontend tests/build and the image/live
  browser checks passed. The audit gate remains unchanged and failing. The
  theme changes no backend code or dependencies.

See [Matrix theme details](matrix-theme.md) for palette and validation scope.

## Completed scheduled full-run audit - 2026-10-03

- Persisted automation run `6525` completed at `09:20:33` Europe/Berlin after
  45,032 seconds (12h 30m 32s), with all 222 regular channels checked. Provider
  refresh completed with zero failed providers/requests; runtime logs contain
  no aborted/failed automation quality stage.
- The run recorded 3,188 analyzed streams: 2,680 good and 508 dead. The dead
  results include overlapping blank/freeze evidence; these classifications are
  media-quality findings rather than failed channel executions. 67 streams were
  revived, two channels were hidden after all their streams failed, and no
  channels were restored. The recorded assigned-stream count is 2,849.
- Specialized preflight work during the long batch rejected 93 expired queued
  entries before media work and one concurrent assignment-order conflict before
  a stale PATCH. Later preflights for the conflicted channel completed, including
  telemetry runs `6522` and `6524`. Regular automation completed successfully;
  preflight subsequently resumed with no current service/Teamarr error.
- No name/import/module/attribute errors appeared in runtime logs. This audit
  records successful execution and source safeguards; it does not establish a
  production speedup from the decomposition or control-plane changes.
