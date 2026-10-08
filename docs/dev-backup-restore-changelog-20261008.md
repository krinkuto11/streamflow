# Development changelog: backup and restore

## Changes

- Replace identifiable channel names in the README screenshots with Sample Channel/example source names.
- Add **Backups** to desktop/mobile System navigation and expose restoration from the setup wizard and during startup initialization.
- Add manual backup, verified ZIP upload, download, inspection, deletion and explicit restore confirmation.
- Add optional automatic daily/weekly/hourly schedules, IANA time zones, persisted next-run timestamps, failure retry delay and configurable retention. Automatic backups default to off.
- Snapshot the live SQLite database through its backup API, preserving committed WAL data without copying database/WAL files independently.
- Include persisted settings, profiles, periods, assignments, provider-specific regex rules and stored connections plus supported JSON configuration.
- Make measurement history optional, including SQL quality telemetry/Analytics/playback observations and bounded in-memory monitoring timelines.
- Apply restoration offline before migrations/database initialization, clear historical monitoring process IDs/active flags, create a safety archive and journal file replacement for recovery after interruption.
- Treat restored SQL as authoritative so stale legacy JSON migration files cannot replace restored connections, profiles or regex rules on later startups.
- Validate archive paths, entry count/size/checksums, JSON, supported schema and SQLite integrity before touching live state.
- Add `BACKUP_DIR` support and entrypoint permissions for an independent persistent backup mount configurable through a normal Unraid DockerMan template.

## Validation

- 57 targeted backend tests cover committed WAL snapshots, settings/credentials/regex round trips, optional history, retention, scheduling/DST, concurrent operations, API confirmations, corrupt/unsafe uploads, partial file replacement, interrupted startup, rollback failures and legacy migration protection.
- Full Ubuntu CI: 2,178 stable backend tests plus 186 subtests, 60 integration tests plus 6 subtests, and 304 frontend tests. Each backend suite has one existing skip. Frontend production build and dependency audits pass; both CodeQL analyses pass.
- Local frontend fixture browser checks: 18 passing checks, including mobile layout/navigation, incomplete setup, startup initialization, upload/download, confirmation/cancellation, pending operations and reconnect behavior. Eight additional browser checks exercise the real running application.
- AMD64 and ARM64 validation images build successfully. Installation and backup storage mapping use the existing Unraid DockerMan container/template.
- Live automatic daily backup completes at the configured time, persists its next run, omits measurement history when requested and leaves the source history unchanged.
- Live restore through the actual UI returns HTTP 202, creates a safety archive, restarts and reconnects automatically. A deliberate backup-setting change is reverted. Hash comparisons verify persistent settings, connection credentials, profiles, regex rules, stored JSON configuration and historical tables against the pre-backup state. Expected runtime differences are the internal restored-SQL marker and the existing preflight attempt-state cleanup performed on startup.
- Live busy-queue restoration is rejected with HTTP 409; a corrupt upload is rejected without leaving an archive.
- A disposable fresh-install data directory on the same Unraid container restores 888 regex patterns and 21,473 quality measurements without a preconfigured connection; normal configuration migration preserves the restored state.
- A separate disposable SQL clone on Unraid captures 1,100 monitoring samples, restores the bounded latest 1,000, keeps the session stopped and consumes the timeline snapshot once. Production monitoring data is not modified by this test.
- Public screenshots contain sample data only. Archives and validation logs containing configuration are kept private.

The Windows full backend run reproduces six pre-existing automation test failures on the unchanged base branch under the same local configuration environment. The Ubuntu CI stable and integration suites pass. Monitoring timeline restoration is bounded and does not introduce continuous persistence across subsequent ordinary restarts.
