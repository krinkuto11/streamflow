# Development changelog: remove built-in backups

StreamFlow archives cannot restore Dispatcharr's independently managed provider and channel inventory. Remove the built-in workflow and preserve existing application data.

## Changes

- Remove the Backups page, setup-wizard restore link, backup API, automatic backup scheduler, monitoring snapshot export and journaled restore startup hooks.
- Remove backup-only request schemas, database helper and development documentation/screenshots.
- Keep the README screenshots with Sample Channel names.
- Preserve the existing SQL-authority migration guard for installations that already have authoritative SQL settings. Legacy JSON files must not overwrite those settings after this removal.
- Leave previously created archive files and persisted user settings intact. Archive creation and restoration are no longer exposed by StreamFlow.

## Validation

- Local backend suite: 2,182 passed, one skipped. Includes the SQL-authority regression and isolated integration contracts.
- Frontend: 302 tests passed; production build passed.
- All six pull-request checks passed, including backend integration contracts, the stable backend suite, frontend build/tests and CodeQL.
- The candidate Docker image built successfully for AMD64 and ARM64. The first attempt encountered an upstream base-image registry rate limit; the retry passed.
- Updated the existing Unraid container through DockerMan. Removed only the backup path and `BACKUP_DIR` template variable; other environment values and container options were preserved.
- Live validation passed: application ready, SQLite integrity intact, existing regex/profile/measurement data unchanged, no pending restore, and existing archive files retained.
- Browser validation passed 25 checks covering existing pages, themes, profile scoring preferences, dialogs, mobile navigation, removal of backup navigation and the setup restore shortcut. The fresh-setup scenario was simulated only in the browser; no application settings were written.
