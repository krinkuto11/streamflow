# Development changelog: remove built-in backups

## Changes

- Remove the Backups page, setup-wizard restore link, backup API, automatic backup scheduler, monitoring snapshot export and journaled restore startup hooks.
- Remove backup-only request schemas, database helper and development documentation/screenshots.
- Keep the README screenshots with Sample Channel names.
- Preserve the existing SQL-authority migration guard for installations that already have authoritative SQL settings. Legacy JSON files must not overwrite those settings after this removal.
- Leave previously created archive files and persisted user settings intact. Archive creation and restoration are no longer exposed by StreamFlow.

## Validation

Local, CI and Unraid DockerMan validation results will be recorded when complete.
