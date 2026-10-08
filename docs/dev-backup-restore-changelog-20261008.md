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
- Validate archive paths, entry count/size/checksums, JSON, supported schema and SQLite integrity before touching live state.
- Add `BACKUP_DIR` support and entrypoint permissions for an independent persistent backup mount configurable through a normal Unraid DockerMan template.

## Validation

Validation results are updated after local, CI and live DockerMan checks complete. Fault-injection tests cover corrupted uploads, partial restores and interrupted startup recovery; public artifacts contain sample data only.
